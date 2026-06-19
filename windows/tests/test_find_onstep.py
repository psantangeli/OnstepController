"""Tests for the Windows discovery wrapper. Run on any OS:

    python -m pytest windows/tests/

Exercises the hosts-file rewrite and confirms the tool reuses the hand
controller's discovery cascade (cache/mDNS/sweep) against a mock OnStep.
"""

import argparse
import os
import socket
import sys
import threading

# Make both the windows/ dir (for find_onstep) and the repo importable.
_WIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_WIN)
sys.path.insert(0, _WIN)
sys.path.insert(0, _ROOT)

import find_onstep  # noqa: E402
from onstep_handset import protocol  # noqa: E402


class MockOnStep:
    """Loopback server that answers :GVP# like OnStep."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                self.sock.settimeout(0.3)
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.settimeout(0.3)
                data = conn.recv(32)
                if protocol.GET_PRODUCT.encode() in data:
                    conn.sendall(b"On-Step#")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


# --- hosts file rewrite ------------------------------------------------------

def test_update_hosts_adds_and_preserves(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1   localhost\n"
                     "192.168.9.9 onstepsws  # do not touch\n")

    assert find_onstep.update_hosts(str(hosts), "onstep", "192.168.4.20") is True
    text = hosts.read_text()
    assert "192.168.4.20\tonstep" in text
    assert "localhost" in text            # preserved
    assert "onstepsws" in text            # similar name NOT clobbered

    # Same IP again -> no change.
    assert find_onstep.update_hosts(str(hosts), "onstep", "192.168.4.20") is False

    # New IP -> exactly one managed 'onstep' line, pointing at the new IP.
    assert find_onstep.update_hosts(str(hosts), "onstep", "10.1.1.7") is True
    onstep_lines = [ln for ln in hosts.read_text().splitlines()
                    if __import__("re").search(r"(^|\s)onstep(\s|$)", ln)]
    assert len(onstep_lines) == 1 and "10.1.1.7" in onstep_lines[0]


# --- discovery reuse ---------------------------------------------------------

def _args(**over):
    base = dict(port=9999, hostnames=[], prefix=24, scan_timeout=0.3,
                retries=1, retry_delay=0, once=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_discover_ip_uses_shared_cache_path(tmp_path, monkeypatch):
    srv = MockOnStep()
    cache = tmp_path / "discovered_host"
    cache.write_text("127.0.0.1\n")
    monkeypatch.setattr(find_onstep, "CACHE_PATH", str(cache))
    try:
        ip = find_onstep.discover_ip(_args(port=srv.port))
        assert ip == "127.0.0.1"          # validated via :GVP# through discovery.py
    finally:
        srv.close()


def test_discover_ip_not_found_returns_none(monkeypatch):
    # Avoid a real LAN sweep: stub the shared discover() to find nothing.
    monkeypatch.setattr(find_onstep.discovery, "discover",
                        lambda **kw: None)
    assert find_onstep.discover_ip(_args()) is None
