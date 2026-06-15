"""Find the OnStep mount on whatever network the handset has joined.

Your SSID/password are constant but the DHCP subnet varies (home/field), so the
mount's IP is not known ahead of time. This module resolves it with a cheap->
expensive cascade and confirms every candidate is really OnStep:

  1. **Cached IP** -- last known-good address (instant if still valid).
  2. **mDNS hostname** -- ``onstep.local`` / ``onstepsws.local``. Works only when
     the mount is an ESP32 with mDNS on (OnStepX default) AND the Pi has avahi/
     libnss-mdns (default on Raspberry Pi OS). Sub-second when it works.
  3. **Subnet sweep** -- scan the local /24 (or detected prefix) for port 9999,
     confirming each open host with ``:GVP#``. Board-agnostic; the reliable path.

Confirmation always uses the real control channel (TCP 9999 + ``:GVP#`` ->
``On-Step#``), so we never hand back a host that isn't actually an OnStep.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import os
import socket
import subprocess

from . import protocol

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
    max_workers: int = 64,
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

    # 3. Subnet sweep.
    network = _local_network(subnet_prefix)
    if network is None:
        log.warning("discovery: could not determine local network; sweep skipped")
        return None
    log.info("discovery: sweeping %s for OnStep on port %d", network, port)
    ip = _sweep(network, port, scan_timeout, max_workers)
    if ip:
        log.info("discovery: found OnStep at %s", ip)
        _write_cache(cache_path, ip)
    else:
        log.warning("discovery: no OnStep found on %s", network)
    return ip


def _resolve_hostname(name: str, port: int) -> str | None:
    try:
        infos = socket.getaddrinfo(name, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        return info[4][0]
    return None


def _sweep(network, port: int, scan_timeout: float, max_workers: int) -> str | None:
    hosts = [str(h) for h in network.hosts()]
    if not hosts:
        return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(identify, h, port, scan_timeout): h for h in hosts}
        try:
            for fut in concurrent.futures.as_completed(futures):
                try:
                    if fut.result():
                        return futures[fut]
                except Exception:  # pragma: no cover - defensive
                    continue
        finally:
            for fut in futures:
                fut.cancel()
    return None


# --- local network detection -------------------------------------------------

def _local_network(default_prefix: int):
    """Best-effort CIDR for the Pi's primary IPv4 interface."""
    ip = _local_ipv4()
    if ip is None:
        return None
    prefix = _prefix_for_ip(ip)
    if prefix is None:
        prefix = default_prefix
    try:
        return ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    except ValueError:
        return None


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


def _prefix_for_ip(ip: str) -> int | None:
    """Read the netmask prefix for ``ip`` from ``ip -o -4 addr`` (Linux)."""
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=2.0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        # e.g. "3: wlan0    inet 192.168.1.42/24 brd ... scope global wlan0"
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
