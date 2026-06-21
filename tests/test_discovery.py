"""Tests for the discovery cascade (identify, cache, hostname, resolver)."""

import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from onstep_handset import discovery, protocol


class TinyOnStep:
    """A loopback server that answers :GVP# like OnStep (for identify())."""

    def __init__(self, product="On-Step#"):
        self.product = product
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
                if protocol.GET_PRODUCT.encode() in data and self.product:
                    conn.sendall(self.product.encode())
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


def test_is_onstep_product():
    assert protocol.is_onstep_product("On-Step#")
    assert protocol.is_onstep_product("OnStepX#")
    assert protocol.is_onstep_product("OnStep#")
    assert not protocol.is_onstep_product("Celestron#")
    assert not protocol.is_onstep_product("")


def test_identify_positive():
    srv = TinyOnStep()
    try:
        assert discovery.identify("127.0.0.1", srv.port, timeout=0.5) is True
    finally:
        srv.close()


def test_identify_wrong_service():
    srv = TinyOnStep(product="SomethingElse#")
    try:
        assert discovery.identify("127.0.0.1", srv.port, timeout=0.5) is False
    finally:
        srv.close()


def test_identify_closed_port():
    # Pick an unused port by binding then closing it.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert discovery.identify("127.0.0.1", port, timeout=0.3) is False


def test_cache_roundtrip(tmp_path):
    cache = str(tmp_path / "host")
    assert discovery._read_cache(cache) is None
    discovery._write_cache(cache, "192.168.5.7")
    assert discovery._read_cache(cache) == "192.168.5.7"


def test_discover_uses_valid_cache(tmp_path):
    srv = TinyOnStep()
    cache = str(tmp_path / "host")
    discovery._write_cache(cache, "127.0.0.1")
    try:
        # Cache points at our server -> discover returns it without sweeping.
        found = discovery.discover(srv.port, hostnames=[], subnet_prefix=24,
                                   scan_timeout=0.3, cache_path=cache)
        assert found == "127.0.0.1"
    finally:
        srv.close()


def test_resolver_fixed_host_skips_discovery():
    r = HostResolver = discovery.HostResolver(
        host="10.0.0.9", port=9999, hostnames=["x"], subnet_prefix=24,
        scan_timeout=0.1, cache_path=None,
    )
    assert r.is_auto is False
    assert r.resolve() == "10.0.0.9"


def test_resolver_auto_flag():
    r = discovery.HostResolver(host="auto", port=9999, hostnames=[],
                               subnet_prefix=24, scan_timeout=0.1, cache_path=None)
    assert r.is_auto is True


def test_sweep_finds_server():
    import ipaddress
    srv = TinyOnStep()
    try:
        # Scan a tiny loopback network that includes 127.0.0.1.
        net = ipaddress.ip_network("127.0.0.0/30")  # .1 and .2
        found = discovery._sweep(net, srv.port, scan_timeout=0.3, max_workers=4)
        assert found == "127.0.0.1"
    finally:
        srv.close()


def test_sweep_no_match():
    import ipaddress
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    net = ipaddress.ip_network("127.0.0.0/30")
    assert discovery._sweep(net, port, scan_timeout=0.2, max_workers=4) is None


# --- multi-adapter sweep -----------------------------------------------------

def test_local_networks_covers_all_adapters(monkeypatch):
    monkeypatch.setattr(discovery, "_local_ipv4_addresses",
                        lambda: {"192.168.1.5", "10.0.0.5", "127.0.0.1", "169.254.7.7"})
    monkeypatch.setattr(discovery, "_prefix_for_ip", lambda ip: None)  # -> default /24
    nets = sorted(str(n) for n in discovery._local_networks(24))
    # Both real adapters; loopback (127.) and APIPA (169.254.) excluded.
    assert nets == ["10.0.0.0/24", "192.168.1.0/24"]


def test_discover_sweeps_every_network(monkeypatch):
    import ipaddress
    n1 = ipaddress.ip_network("192.168.1.0/24")
    n2 = ipaddress.ip_network("10.0.0.0/24")
    monkeypatch.setattr(discovery, "_local_networks", lambda prefix: [n1, n2])
    swept = []

    def fake_sweep(network, port, scan_timeout, max_workers):
        swept.append(str(network))
        return "10.0.0.42" if network == n2 else None   # OnStep is on the 2nd adapter

    monkeypatch.setattr(discovery, "_sweep", fake_sweep)
    found = discovery.discover(9999, hostnames=[], subnet_prefix=24, cache_path=None)
    assert found == "10.0.0.42"
    assert swept == ["192.168.1.0/24", "10.0.0.0/24"]   # tried both, in order
