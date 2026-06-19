"""Integration tests for comms + the CommsWorker against a mock OnStep server.

These run off-Pi (no GPIO/LCD) and exercise: connect, query/parse, command
dispatch from the action queue, rate cycling, and reconnect after a drop.
"""

import os
import queue
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from onstep_handset import inputs as inp
from onstep_handset import protocol
from onstep_handset.comms import OnStepClient
from onstep_handset.config import Config
from onstep_handset.discovery import HostResolver
from onstep_handset.main import CommsWorker
from onstep_handset.state import MountState, SharedState


class MockOnStep:
    """Minimal LX200 server: answers :GU#/:GR#/:GD#, records all commands."""

    def __init__(self, port: int = 0):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                self.sock.settimeout(0.5)
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self._handle(conn)

    def _handle(self, conn):
        conn.settimeout(0.5)
        buf = b""
        while not self._stop.is_set():
            try:
                data = conn.recv(64)
            except socket.timeout:
                continue
            except OSError:
                break
            if data == b"":
                break
            buf += data
            while b"#" in buf:
                cmd, _, buf = buf.partition(b"#")
                cmd = (cmd + b"#").decode()
                self.received.append(cmd)
                reply = self._reply(cmd)
                if reply:
                    conn.sendall(reply.encode())
        conn.close()

    def _reply(self, cmd: str) -> str:
        if cmd == protocol.GET_STATUS:
            return "Np0#"          # tracking, not slewing, not parked, no error
        if cmd == protocol.GET_RA:
            return "12:34:56#"
        if cmd == protocol.GET_DEC:
            return "+41*16:09#"
        if cmd == protocol.GET_PRODUCT:
            return "On-Step#"
        if cmd == protocol.GET_VERSION:
            return "10.28n#"
        return ""                  # motion/rate/track commands: no reply

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def _config(port: int) -> Config:
    return Config(
        host="127.0.0.1", port=port, connect_timeout=1.0,
        backoff_min=0.05, backoff_max=0.2, poll_hz=20.0, ui_fps=5.0,
        spi_hz=32_000_000, rotation=0, slew_rates=["RG", "RC", "RM", "RS"],
        default_rate_index=1, pins={},
    )


def test_client_query_roundtrip():
    server = MockOnStep()
    try:
        client = OnStepClient("127.0.0.1", server.port, timeout=1.0)
        assert client.connect()
        assert client.query(":GR#") == "12:34:56#"
        assert client.query(":GU#") == "Np0#"
        client.close()
    finally:
        server.close()


def _run_worker(cfg, server, settings_path=None):
    shared = SharedState(MountState())
    actions: "queue.Queue" = queue.Queue()
    stop = threading.Event()
    # Fixed-host resolver -> always returns cfg.host (no discovery in these tests).
    resolver = HostResolver(host=cfg.host, port=cfg.port, hostnames=[],
                            subnet_prefix=24, scan_timeout=0.2, cache_path=None)
    client = OnStepClient(None, cfg.port, timeout=cfg.connect_timeout,
                          backoff_min=cfg.backoff_min, backoff_max=cfg.backoff_max)
    # Default to a throwaway settings path so tests never touch the real file.
    settings_path = settings_path or os.path.join(
        os.path.dirname(__file__), ".test_ui_settings.json")
    worker = CommsWorker(cfg, client, resolver, shared, actions, stop,
                         settings_path=settings_path)
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    return shared, actions, stop, t


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_worker_polls_and_populates_state():
    server = MockOnStep()
    cfg = _config(server.port)
    shared, actions, stop, t = _run_worker(cfg, server)
    try:
        assert _wait(lambda: shared.snapshot().connected
                     and shared.snapshot().ra == "12h 34m 56s")
        st = shared.snapshot()
        assert st.dec == "+41° 16' 09\""
        assert st.tracking is True
        assert st.slewing is False
        assert st.rate_label == "Center 8x"
    finally:
        stop.set(); t.join(timeout=2.0); server.close()


def test_worker_dispatches_move_and_stop():
    server = MockOnStep()
    cfg = _config(server.port)
    shared, actions, stop, t = _run_worker(cfg, server)
    try:
        assert _wait(lambda: shared.snapshot().connected)
        actions.put(inp.Action(inp.MOVE, "n"))
        actions.put(inp.Action(inp.STOP, "n"))
        actions.put(inp.Action(inp.STOP_ALL))
        assert _wait(lambda: ":Mn#" in server.received
                     and ":Qn#" in server.received
                     and ":Q#" in server.received)
    finally:
        stop.set(); t.join(timeout=2.0); server.close()


def test_worker_rate_cycle():
    server = MockOnStep()
    cfg = _config(server.port)
    shared, actions, stop, t = _run_worker(cfg, server)
    try:
        assert _wait(lambda: shared.snapshot().connected)
        actions.put(inp.Action(inp.RATE_UP))     # RC -> RM
        assert _wait(lambda: shared.snapshot().rate_label == "Find 20x")
        actions.put(inp.Action(inp.RATE_DOWN))   # RM -> RC
        actions.put(inp.Action(inp.RATE_DOWN))   # RC -> RG
        assert _wait(lambda: shared.snapshot().rate_label == "Guide 1x")
        # Clamps at slowest.
        actions.put(inp.Action(inp.RATE_DOWN))
        time.sleep(0.2)
        assert shared.snapshot().rate_label == "Guide 1x"
    finally:
        stop.set(); t.join(timeout=2.0); server.close()


def test_worker_tracking_via_menu():
    server = MockOnStep()
    cfg = _config(server.port)  # tracking_modes default: off/sidereal/solar/lunar
    shared, actions, stop, t = _run_worker(cfg, server)
    try:
        assert _wait(lambda: shared.snapshot().connected)
        assert shared.snapshot().tracking_mode == "Off"      # starts off
        # Open the menu (KEY2); "Tracking" is the first row (menu_index 0).
        actions.put(inp.Action(inp.MENU))
        assert _wait(lambda: shared.snapshot().menu_open)
        # Joystick right cycles the tracking rate forward.
        actions.put(inp.Action(inp.MOVE, "e"))               # -> sidereal
        assert _wait(lambda: shared.snapshot().tracking_mode == "Sidereal")
        assert _wait(lambda: ":TQ#" in server.received and ":Te#" in server.received)
        actions.put(inp.Action(inp.MOVE, "e"))               # -> solar
        assert _wait(lambda: shared.snapshot().tracking_mode == "Solar")
        assert _wait(lambda: ":TS#" in server.received)
        # Left steps backward (solar -> sidereal).
        actions.put(inp.Action(inp.MOVE, "w"))
        assert _wait(lambda: shared.snapshot().tracking_mode == "Sidereal")
    finally:
        stop.set(); t.join(timeout=2.0); server.close()


def test_worker_menu_and_brightness(tmp_path):
    server = MockOnStep()
    cfg = _config(server.port)   # default_brightness_index=1, levels [.35,.65,1.0]
    settings = str(tmp_path / "ui.json")
    shared, actions, stop, t = _run_worker(cfg, server, settings_path=settings)
    try:
        assert _wait(lambda: shared.snapshot().connected)
        assert shared.snapshot().brightness_index == 1
        assert shared.snapshot().menu_open is False

        # Open the settings menu (KEY2); motion is stopped for safety.
        actions.put(inp.Action(inp.MENU))
        assert _wait(lambda: shared.snapshot().menu_open)
        assert _wait(lambda: ":Q#" in server.received)

        # Brightness is the second row -- joystick down selects it.
        actions.put(inp.Action(inp.MOVE, "s"))
        assert _wait(lambda: shared.snapshot().menu_index == 1)

        # Joystick right cycles brightness up (1 -> 2) and persists it.
        actions.put(inp.Action(inp.MOVE, "e"))
        assert _wait(lambda: shared.snapshot().brightness_index == 2)
        import json
        assert json.load(open(settings))["brightness_index"] == 2

        # Right again wraps 2 -> 0.
        actions.put(inp.Action(inp.MOVE, "e"))
        assert _wait(lambda: shared.snapshot().brightness_index == 0)

        # While the menu is open, joystick directions must NOT slew the mount.
        actions.put(inp.Action(inp.MOVE, "n"))
        time.sleep(0.2)
        assert ":Mn#" not in server.received

        # Close the menu (KEY2 again).
        actions.put(inp.Action(inp.MENU))
        assert _wait(lambda: shared.snapshot().menu_open is False)
    finally:
        stop.set(); t.join(timeout=2.0); server.close()


def test_brightness_persists_across_workers(tmp_path):
    server = MockOnStep()
    cfg = _config(server.port)
    settings = str(tmp_path / "ui.json")
    shared, actions, stop, t = _run_worker(cfg, server, settings_path=settings)
    try:
        assert _wait(lambda: shared.snapshot().connected)
        actions.put(inp.Action(inp.MENU))
        assert _wait(lambda: shared.snapshot().menu_open)
        actions.put(inp.Action(inp.MOVE, "s"))           # select Brightness (row 1)
        assert _wait(lambda: shared.snapshot().menu_index == 1)
        actions.put(inp.Action(inp.MOVE, "e"))           # 1 -> 2
        assert _wait(lambda: shared.snapshot().brightness_index == 2)
    finally:
        stop.set(); t.join(timeout=2.0)
    # A fresh worker loads the persisted brightness.
    shared2, actions2, stop2, t2 = _run_worker(cfg, server, settings_path=settings)
    try:
        assert _wait(lambda: shared2.snapshot().brightness_index == 2)
    finally:
        stop2.set(); t2.join(timeout=2.0); server.close()


def test_worker_reconnects_after_drop():
    server = MockOnStep()
    port = server.port
    cfg = _config(port)
    shared, actions, stop, t = _run_worker(cfg, server)
    server2 = None
    try:
        assert _wait(lambda: shared.snapshot().connected)
        server.close()                            # kill the mount
        assert _wait(lambda: shared.snapshot().connected is False, timeout=3.0)
        # Mount returns on the same port; worker should reconnect via backoff.
        server2 = MockOnStep(port=port)
        assert _wait(lambda: shared.snapshot().connected
                     and shared.snapshot().ra == "12h 34m 56s", timeout=4.0)
    finally:
        stop.set(); t.join(timeout=2.0); server.close()
        if server2 is not None:
            server2.close()
