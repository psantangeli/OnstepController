#!/usr/bin/env python3
r"""Discover the OnStep mount and pin a stable hostname ("onstep") to its current
IP via the Windows hosts file, so ASCOM / NINA / PHD2 can connect by name no
matter which subnet you're on.

This reuses the EXACT discovery code from the hand controller
(``onstep_handset.discovery``) -- the same cascade that works on the Pi:

    1. Cached IP   (last known-good, validated instantly)
    2. mDNS        (onstep.local / onstepsws.local)
    3. Subnet sweep of TCP 9999, confirming each candidate with :GVP# -> On-Step#

Editing the hosts file needs Administrator, so the script self-elevates (UAC).

Usage (on the telescope PC, Python 3 installed):
    python windows\find_onstep.py            # discover now + update hosts
    python windows\find_onstep.py --install  # run at logon (Task Scheduler)
    python windows\find_onstep.py --uninstall
    python windows\find_onstep.py --once     # don't loop/retry; single attempt

Then point the ASCOM OnStep/LX200 driver at host "onstep", port 9999.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time

# Reuse the hand controller's proven discovery module (stdlib-only, no luma/gpio).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from onstep_handset import discovery  # noqa: E402

log = logging.getLogger("find_onstep")

MARKER = "# OnStep auto-discovery"
TASK_NAME = "OnStep Discovery"
_DATA_DIR = os.path.join(os.environ.get("ProgramData", _REPO_ROOT), "OnStep")
CACHE_PATH = os.path.join(_DATA_DIR, "discovered_host")
LOG_PATH = os.path.join(_DATA_DIR, "discovery.log")


def hosts_path() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(root, "System32", "drivers", "etc", "hosts")


# --- hosts file --------------------------------------------------------------

def update_hosts(path: str, hostname: str, ip: str) -> bool:
    """Rewrite the single managed line for ``hostname``. Returns True if changed.

    Preserves every other line and does NOT clobber similar names (e.g.
    ``onstepsws``). Pure file I/O -- unit-testable on any OS.
    """
    original: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            original = fh.read().splitlines()

    alias_re = re.compile(r"(^|\s)" + re.escape(hostname) + r"(\s|$)")
    kept = [ln for ln in original if MARKER not in ln and not alias_re.search(ln)]
    updated = kept + [f"{ip}\t{hostname}\t{MARKER}"]
    if updated == original:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(updated) + "\n")
    return True


# --- Windows elevation / scheduled task --------------------------------------

def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate() -> bool:
    """Relaunch this script elevated (UAC). Returns True if launched."""
    import ctypes
    params = " ".join(f'"{a}"' for a in sys.argv)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    return int(rc) > 32


def install_task() -> None:
    script = os.path.abspath(__file__)
    # Prefer pythonw.exe so no console window pops up at logon.
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    run = f'"{exe}" "{script}"'
    subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", run,
         "/SC", "ONLOGON", "/RL", "HIGHEST", "/DELAY", "0000:15", "/F"],
        check=True)
    log.info("installed scheduled task '%s' (runs at logon, elevated)", TASK_NAME)


def uninstall_task() -> None:
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False)
    log.info("removed scheduled task '%s'", TASK_NAME)


# --- main --------------------------------------------------------------------

def _setup_logging(quiet: bool) -> None:
    handlers: list[logging.Handler] = []
    if not quiet:
        handlers.append(logging.StreamHandler())
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_PATH))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")


def discover_ip(args) -> str | None:
    """Run the shared discovery cascade, retrying while the network comes up."""
    attempts = 1 if args.once else args.retries
    for i in range(1, attempts + 1):
        ip = discovery.discover(
            port=args.port,
            hostnames=args.hostnames,
            subnet_prefix=args.prefix,
            scan_timeout=args.scan_timeout,
            cache_path=CACHE_PATH,
        )
        if ip:
            return ip
        if i < attempts:
            log.warning("OnStep not found (attempt %d/%d); retrying in %ds "
                        "(network may still be coming up)", i, attempts, args.retry_delay)
            time.sleep(args.retry_delay)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover OnStep and pin a hosts entry")
    parser.add_argument("--hostname", default="onstep", help="hosts alias (default: onstep)")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--hostnames", nargs="*",
                        default=["onstep.local", "onstepsws.local"],
                        help="mDNS names to try before sweeping")
    parser.add_argument("--prefix", type=int, default=24,
                        help="subnet prefix to sweep when the netmask can't be "
                             "detected (default /24)")
    parser.add_argument("--scan-timeout", type=float, default=0.3, dest="scan_timeout")
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--retry-delay", type=int, default=3, dest="retry_delay")
    parser.add_argument("--once", action="store_true", help="single attempt, no retry")
    parser.add_argument("--install", action="store_true", help="register logon task")
    parser.add_argument("--uninstall", action="store_true", help="remove logon task")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.quiet)

    # Everything below needs admin (hosts file / scheduled task). Self-elevate.
    if os.name == "nt" and not is_admin():
        log.info("not elevated; relaunching with Administrator rights...")
        return 0 if elevate() else 3

    if args.install:
        install_task()
        return 0
    if args.uninstall:
        uninstall_task()
        return 0

    ip = discover_ip(args)
    if not ip:
        log.error("OnStep not found on the local network.")
        return 1

    try:
        changed = update_hosts(hosts_path(), args.hostname, ip)
    except OSError as exc:
        log.error("could not write hosts file (%s): %s -- are you elevated?",
                  hosts_path(), exc)
        return 2
    if changed:
        log.info("hosts updated: %s -> %s", ip, args.hostname)
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True)
    else:
        log.info("hosts already current: %s -> %s", ip, args.hostname)
    log.info("OnStep reachable as '%s' (%s:%d). Point ASCOM/NINA at host '%s'.",
             args.hostname, ip, args.port, args.hostname)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
