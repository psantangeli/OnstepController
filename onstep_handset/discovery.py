"""Find the OnStep mount on whatever network the handset has joined.

Your SSID/password are constant but the DHCP subnet varies (home/field), so the
mount's IP is not known ahead of time. This module resolves it with a cheap->
expensive cascade and confirms every candidate is really OnStep:

  1. **Cached IP** -- last known-good address (instant if still valid).
  2. **mDNS hostname** -- ``onstep.local`` / ``onstepsws.local``. Works only when
     the mount is an ESP32 with mDNS on (OnStepX default) AND the Pi has avahi/
     libnss-mdns (default on Raspberry Pi OS). Sub-second when it works.
  3. **Subnet sweep** -- scan EVERY local IPv4 adapter's network (a host PC may
     have several, e.g. two WiFi adapters) for port 9999, confirming each open
     host with ``:GVP#``. Board-agnostic; the reliable path. The scan is
     single-threaded (one non-blocking ``select`` loop), so it stays gentle on
     the Pi Zero's single ARMv6 core -- no thread-pool CPU storm.

Confirmation always uses the real control channel (TCP 9999 + ``:GVP#`` ->
``On-Step#``), so we never hand back a host that isn't actually an OnStep.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import os
import selectors
import socket
import subprocess
import time

from . import protocol

# connect_ex() return codes meaning "connection in progress" (non-blocking).
_INPROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK}
if hasattr(errno, "WSAEWOULDBLOCK"):       # Windows
    _INPROGRESS.add(errno.WSAEWOULDBLOCK)

log = logging.getLogger(__name__)


# --- candidate confirmation --------------------------------------------------

def identify(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if ``host:port`` answers ``:GVP#`` as an OnStep device.

    A closed port / wrong service / silence all return False quickly. A leading
    ``#`` is sent first to clear any partial LX200 state on the channel.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"#" + protocol.GET_PRODUCT.encode("ascii"))
            reply = _read_until_hash(sock, max_bytes=64)
    except OSError:
        return False
    return protocol.is_onstep_product(reply)


def _read_until_hash(sock: socket.socket, max_bytes: int = 64) -> str:
    chunks: list[bytes] = []
    for _ in range(max_bytes):
        try:
            b = sock.recv(1)
        except OSError:
            break
        if b in (b"", None):
            break
        chunks.append(b)
        if b == b"#":
            break
    return b"".join(chunks).decode("ascii", errors="replace")


# --- discovery cascade -------------------------------------------------------

def discover(
    port: int,
    hostnames: list[str],
    subnet_prefix: int = 24,
    scan_timeout: float = 0.3,
    cache_path: str | None = None,
    max_workers: int = 256,
) -> str | None:
    """Resolve the OnStep IP. Returns the address, or None if not found.

    On success the address is written to ``cache_path`` (if given) for next time.
    """
    # 1. Cached IP.
    cached = _read_cache(cache_path)
    if cached and identify(cached, port, timeout=scan_timeout * 3):
        log.info("discovery: cached host %s still valid", cached)
        return cached

    # 2. mDNS / hostname resolution.
    for name in hostnames:
        ip = _resolve_hostname(name, port)
        if ip and identify(ip, port, timeout=scan_timeout * 3):
            log.info("discovery: resolved %s -> %s", name, ip)
            _write_cache(cache_path, ip)
            return ip

    # 3. Subnet sweep -- across EVERY local IPv4 adapter, not just the default
    #    route (a host PC may have e.g. two WiFi adapters on different subnets).
    networks = _local_networks(subnet_prefix)
    if not networks:
        log.warning("discovery: could not determine any local network; sweep skipped")
        return None
    for network in networks:
        log.info("discovery: sweeping %s for OnStep on port %d", network, port)
        ip = _sweep(network, port, scan_timeout, max_workers)
        if ip:
            log.info("discovery: found OnStep at %s", ip)
            _write_cache(cache_path, ip)
            return ip
    log.warning("discovery: no OnStep found on %s", ", ".join(str(n) for n in networks))
    return None


def _resolve_hostname(name: str, port: int) -> str | None:
    try:
        infos = socket.getaddrinfo(name, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        return info[4][0]
    return None


def _sweep(network, port: int, scan_timeout: float, max_workers: int) -> str | None:
    """Find an OnStep on the network with minimal CPU.

    Single-threaded: a non-blocking ``connect`` scan (one ``select`` loop, gentle
    on a single-core Pi Zero -- no thread pool) finds which hosts have the port
    open, then the few open hosts are confirmed sequentially with ``:GVP#``.
    ``max_workers`` bounds how many sockets are opened at once (fd safety).
    """
    hosts = [str(h) for h in network.hosts()]
    if not hosts:
        return None
    for batch in _chunks(hosts, max(1, max_workers)):
        for ip in _open_ports(batch, port, scan_timeout):
            if identify(ip, port, timeout=scan_timeout * 3):
                return ip
    return None


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _open_ports(hosts, port: int, timeout: float) -> list[str]:
    """Hosts (of ``hosts``) with ``port`` open, via one non-blocking select loop."""
    sel = selectors.DefaultSelector()
    pending: dict = {}
    opened: list[str] = []
    try:
        for ip in hosts:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                rc = sock.connect_ex((ip, port))
            except OSError:
                sock.close()
                continue
            if rc == 0:                       # connected immediately
                opened.append(ip)
                sock.close()
            elif rc in _INPROGRESS:           # will complete asynchronously
                sel.register(sock, selectors.EVENT_WRITE, ip)
                pending[sock] = ip
            else:                             # refused / unreachable right away
                sock.close()

        deadline = time.monotonic() + timeout
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            events = sel.select(timeout=remaining)
            if not events:
                break
            for key, _mask in events:
                sock, ip = key.fileobj, key.data
                # SO_ERROR == 0 means the connection succeeded (port open).
                if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    opened.append(ip)
                sel.unregister(sock)
                sock.close()
                pending.pop(sock, None)
    finally:
        for sock in list(pending):
            try:
                sel.unregister(sock)
            except Exception:
                pass
            sock.close()
        sel.close()
    return opened


# --- local network detection -------------------------------------------------

def _local_networks(default_prefix: int) -> list:
    """CIDR networks for EVERY local IPv4 adapter (one PC may have several, e.g.
    two WiFi adapters on different subnets). Loopback/APIPA are skipped. Dedupes
    overlapping networks. Prefix from the OS where we can read it, else default."""
    nets: dict = {}
    for ip in _local_ipv4_addresses():
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        prefix = _prefix_for_ip(ip) or default_prefix
        try:
            net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
        except ValueError:
            continue
        nets.setdefault(str(net), net)
    return list(nets.values())


def _local_ipv4_addresses() -> set:
    """Best-effort set of all local IPv4 addresses across every adapter."""
    ips: set = set()
    # Default-route source IP (the one adapter the connect-trick picks).
    dr = _local_ipv4()
    if dr:
        ips.add(dr)
    # Every address bound to the hostname -- on Windows this returns all adapters.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # Linux: `ip -o -4 addr` enumerates everything reliably (incl. prefixes).
    ips.update(_ip_cmd_addresses())
    return ips


def _local_ipv4() -> str | None:
    """Source IPv4 used to reach off-link destinations (no packets actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1; UDP connect just picks a route
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _ip_cmd_lines() -> list:
    """Lines of `ip -o -4 addr show` (Linux), or [] elsewhere/on failure."""
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=2.0,
        ).stdout
        return out.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def _ip_cmd_addresses() -> set:
    """All IPv4 addresses from `ip -o -4 addr` (Linux). Empty elsewhere."""
    found: set = set()
    for line in _ip_cmd_lines():
        # e.g. "3: wlan0    inet 192.168.1.42/24 brd ... scope global wlan0"
        toks = line.split()
        if "inet" in toks:
            cidr = toks[toks.index("inet") + 1] if toks.index("inet") + 1 < len(toks) else ""
            if "/" in cidr:
                found.add(cidr.split("/", 1)[0])
    return found


def _prefix_for_ip(ip: str) -> int | None:
    """Netmask prefix for ``ip`` from `ip -o -4 addr` (Linux); None elsewhere."""
    for line in _ip_cmd_lines():
        for token in line.split():
            if token.startswith(f"{ip}/"):
                try:
                    return int(token.split("/", 1)[1])
                except ValueError:
                    return None
    return None


# --- cache -------------------------------------------------------------------

def _read_cache(path: str | None) -> str | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        return value or None
    except OSError:
        return None


def _write_cache(path: str | None, ip: str) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(ip + "\n")
    except OSError as exc:  # pragma: no cover - non-fatal
        log.debug("discovery: could not write cache %s: %s", path, exc)


class HostResolver:
    """Wraps config: a fixed IP is returned as-is; ``auto`` runs :func:`discover`."""

    def __init__(self, host, port, hostnames, subnet_prefix, scan_timeout,
                 cache_path, use_cache=True):
        self.host = host
        self.port = port
        self.hostnames = hostnames
        self.subnet_prefix = subnet_prefix
        self.scan_timeout = scan_timeout
        self.cache_path = cache_path if use_cache else None

    @property
    def is_auto(self) -> bool:
        return str(self.host).strip().lower() == "auto"

    def resolve(self, force: bool = False) -> str | None:
        """Return the OnStep IP. ``force`` ignores the cache (used after repeated
        connect failures, e.g. when the Pi has moved to a different network)."""
        if not self.is_auto:
            return self.host
        cache_path = None if force else self.cache_path
        return discover(self.port, self.hostnames, self.subnet_prefix,
                        self.scan_timeout, cache_path)
