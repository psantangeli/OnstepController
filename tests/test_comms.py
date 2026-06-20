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
        # Tracking enable/disable/rate commands DO reply "1#" on real OnStep --
        # exercise the client's stale-reply draining.
        if cmd in (":Te#", ":Td#", ":TQ#", ":TS#", ":TL#", ":TK#"):
            return "1#"
        return ""                  # motion / rate / home commands: no reply

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


def test_worker_park_requires_confirm(tmp_path):
    server = MockOnStep()
    cfg = _config(server.port)
    settings = str(tmp_path / "ui.json")
    shared, actions, stop, t = _run_worker(cfg, server, settings_path=settings)
    try:
        assert _wait(lambda: shared.snapshot().connected)
        actions.put(inp.Action(inp.MENU))                       # open menu
        assert _wait(lambda: shared.snapshot().menu_open)
        actions.put(inp.Action(inp.MOVE, "s"))                  # -> Brightness
        actions.put(inp.Action(inp.MOVE, "s"))                  # -> Park (row 2)
        assert _wait(lambda: shared.snapshot().menu_index == 2)

        # First right arms the confirm; nothing sent yet.
        actions.put(inp.Action(inp.MOVE, "e"))
        assert _wait(lambda: shared.snapshot().menu_confirm)
        time.sleep(0.15)
        assert ":hC#" not in server.received

        # Second right runs Park: :hC# + :Td#, tracking off, menu closes.
        actions.put(inp.Action(inp.MOVE, "e"))
        assert _wait(lambda: ":hC#" in server.received)
        assert _wait(lambda: ":Td#" in server.received)
        assert _wait(lambda: shared.snapshot().menu_open is False)
        assert shared.snapshot().tracking_mode == "Off"
    finally:
        stop.set(); t.join(timeout=2.0); server.close()


def test_worker_park_confirm_can_be_cancelled(tmp_path):
    server = MockOnStep()
    cfg = _config(server.port)
    settings = str(tmp_path / "ui.json")
    shared, actions, stop, t = _run_worker(cfg, server, settings_path=settings)
    try:
        assert _wait(lambda: shared.snapshot().connected)
        actions.put(inp.Action(inp.MENU))
        assert _wait(lambda: shared.snapshot().menu_open)
        actions.put(inp.Action(inp.MOVE, "s"))
        actions.put(inp.Action(inp.MOVE, "s"))                  # -> Park
        assert _wait(lambda: shared.snapshot().menu_index == 2)
        actions.put(inp.Action(inp.MOVE, "e"))                  # arm
        assert _wait(lambda: shared.snapshot().menu_confirm)
        actions.put(inp.Action(inp.MOVE, "w"))                  # left cancels
        assert _wait(lambda: shared.snapshot().menu_confirm is False)
        time.sleep(0.15)
        assert ":hC#" not in server.received                    # never ran
        assert shared.snapshot().menu_open is True              # still in menu
    finally:
        stop.set(); t.join(timeout=2.0); server.close()


def _open_menu_at(actions, shared, index):
    actions.put(inp.Action(inp.MENU))
    assert _wait(lambda: shared.snapshot().menu_open)
    for _ in range(index):
        actions.put(inp.Action(inp.MOVE, "s"))
    assert _wait(lambda: shared.snapshot().menu_index == index)


def test_worker_update_applies_and_exits(tmp_path, monkeypatch):
    import onstep_handset.firmware as fw
    import onstep_handset.main as m
    monkeypatch.setattr(fw, "update",
                        lambda repo_dir=None: fw.UpdateResult(True, True, "Updated"))
    monkeypatch.setattr(fw, "under_systemd", lambda: True)
    monkeypatch.setattr(m, "_UPDATE_RESTART_DELAY", 0.05)
    server = MockOnStep()
    cfg = _config(server.port)
    shared, actions, stop, t = _run_worker(cfg, server, settings_path=str(tmp_path / "ui.json"))
    try:
        assert _wait(lambda: shared.snapshot().connected)
        _open_menu_at(actions, shared, 3)            # Update is row 3
        actions.put(inp.Action(inp.MOVE, "e"))       # arm
        assert _wait(lambda: shared.snapshot().menu_confirm)
        actions.put(inp.Action(inp.MOVE, "e"))       # run
        # Under systemd, a successful update exits the app (systemd relaunches).
        assert _wait(lambda: stop.is_set(), timeout=3.0)
    finally:
        stop.set(); t.join(timeout=2.0); server.close()


def test_worker_update_up_to_date_stays_running(tmp_path, monkeypatch):
    import onstep_handset.firmware as fw
    import onstep_handset.main as m
    monkeypatch.setattr(fw, "update",
                        lambda repo_dir=None: fw.UpdateResult(True, False, "Already up to date"))
    monkeypatch.setattr(m, "_UPDATE_RESULT_DELAY", 0.1)
    server = MockOnStep()
    cfg = _config(server.port)
    shared, actions, stop, t = _run_worker(cfg, server, settings_path=str(tmp_path / "ui.json"))
    try:
        assert _wait(lambda: shared.snapshot().connected)
        _open_menu_at(actions, shared, 3)
        actions.put(inp.Action(inp.MOVE, "e"))       # arm
        actions.put(inp.Action(inp.MOVE, "e"))       # run
        assert _wait(lambda: shared.snapshot().update_msg == "Already up to date")
        # Message clears and the app keeps running (not exited).
        assert _wait(lambda: shared.snapshot().update_msg == "")
        assert not stop.is_set()
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


def test_menu_opens_while_disconnected(tmp_path):
    # Point the worker at a dead port so it never connects and stays searching.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    cfg = _config(dead_port)
    shared, actions, stop, t = _run_worker(cfg, None, settings_path=str(tmp_path / "ui.json"))
    try:
        # It cannot connect...
        time.sleep(0.3)
        assert shared.snapshot().connected is False
        # ...but KEY2 still opens the settings menu (input handled while searching).
        actions.put(inp.Action(inp.MENU))
        assert _wait(lambda: shared.snapshot().menu_open, timeout=3.0)
        # And closing it returns to searching.
        actions.put(inp.Action(inp.MENU))
        assert _wait(lambda: shared.snapshot().menu_open is False)
    finally:
        stop.set(); t.join(timeout=2.0)


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
