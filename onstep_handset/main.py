"""Entry point: wires inputs -> command queue -> comms thread -> shared state -> UI.

Threading model (single ARMv6 core, but socket I/O releases the GIL):
  * comms thread  -- owns the only socket; drains the action queue (writes
    move/stop/rate commands) and polls :GU#/:GR#/:GD# on an interval.
  * main/UI thread -- reads state snapshots and repaints the LCD on change.

GPIO callbacks run on gpiozero's own threads but only enqueue Actions, so all
network writes stay serialised on the comms thread.
"""

from __future__ import annotations

import argparse
import logging
import queue
import signal
import threading
import time

from . import protocol
from .comms import OnStepClient
from .config import HOST_CACHE, SETTINGS_PATH, Config, load
from .discovery import HostResolver
from .inputs import (Action, InputController, MENU, MOVE, RATE_DOWN, RATE_UP,
                     STOP, STOP_ALL)
from .display import ACTION_ITEMS, MENU_ITEMS
from .settings import load_settings, save_settings
from .state import MountState, SharedState

log = logging.getLogger("onstep_handset")

#: Consecutive connect failures on a known host before we re-run discovery
#: (handles the Pi having moved to a different network / the mount's DHCP IP
#: having changed). Deliberately high: while we have a known-good IP we just keep
#: reconnecting to it (one connection attempt each), rather than re-sweeping --
#: re-discovery does an extra :GVP# probe per cycle, which only adds load to the
#: OnStep's limited TCP slots and can race the real connect for the last slot.
REDISCOVER_AFTER = 12

#: How long the software-update screen lingers before restart / clearing.
_UPDATE_RESTART_DELAY = 1.5
_UPDATE_RESULT_DELAY = 2.5


def _wifi_ssid() -> str:
    """Best-effort current WiFi SSID (Linux/Pi via iwgetid). '' if unknown."""
    import subprocess
    try:
        out = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True,
                             timeout=2.0)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class CommsWorker:
    """Runs on the comms thread: command dispatch + status polling + reconnect."""

    def __init__(self, cfg: Config, client: OnStepClient, resolver: HostResolver,
                 shared: SharedState, actions: "queue.Queue[Action]",
                 stop_event: threading.Event, settings_path: str | None = None) -> None:
        self.cfg = cfg
        self.client = client
        self.resolver = resolver
        self.shared = shared
        self.actions = actions
        self.stop_event = stop_event
        self.rate_index = cfg.default_rate_index
        self.tracking_index = cfg.default_tracking_index
        self._poll_interval = 1.0 / cfg.poll_hz
        self._fail_count = 0
        # UI / settings menu state.
        self._settings_path = settings_path or SETTINGS_PATH
        persisted = load_settings(self._settings_path)
        self.brightness_index = self._clamp_brightness(
            persisted.get("brightness_index", cfg.default_brightness_index))
        self.menu_open = False
        self.menu_index = 0
        self.menu_confirm = False
        self._next_netinfo = 0.0   # when to next refresh the Pi's own IP/SSID

    def run(self) -> None:
        self.shared.update(rate_index=self.rate_index,
                           rate_label=self._rate_label(),
                           tracking_mode=self._tracking_label(),
                           brightness_index=self.brightness_index)
        next_poll = 0.0
        while not self.stop_event.is_set():
            # ALWAYS handle input first, so the buttons work even while the mount
            # is missing -- otherwise a search loop locks the user out.
            self._drain_actions()

            # While the settings menu is open, pause everything mount-related (no
            # discovery, no connect, no polling). This is the "stop scanning" the
            # user can trigger any time by pressing KEY2.
            if self.menu_open:
                time.sleep(0.02)
                continue

            if not self.client.connected:
                self._publish_self_network()   # show the Pi's own IP/SSID while searching
                # (Re)discover the mount if we have no target, or the current one
                # has failed repeatedly (we may have changed networks).
                if not self.client.host or self._fail_count >= REDISCOVER_AFTER:
                    if not self._discover():
                        self._idle_wait(self.client.next_backoff())
                        continue
                if not self.client.connect():
                    self._fail_count += 1
                    self.shared.update(connected=False)
                    self._idle_wait(self.client.next_backoff())
                    continue
                self._fail_count = 0
                self.shared.update(connected=True, searching=False,
                                   host=self.client.host or "")
                # Push current rate to the mount on (re)connect.
                self._safe(lambda: self.client.send(protocol.rate(self._rate_code())))
                next_poll = 0.0
                continue

            now = time.monotonic()
            if now >= next_poll:
                self._poll_status()
                next_poll = now + self._poll_interval

            # Short sleep: stay responsive to the action queue without busy-spin.
            time.sleep(0.01)

        # Best-effort stop-all on shutdown.
        self._safe(lambda: self.client.send(protocol.STOP_ALL))
        self.client.close()

    def _idle_wait(self, duration: float) -> None:
        """Wait up to ``duration`` seconds while still handling input, so a button
        press (e.g. KEY2 to open the menu) is acted on promptly instead of being
        stuck behind a long reconnect backoff."""
        end = time.monotonic() + duration
        while not self.stop_event.is_set():
            self._drain_actions()
            if self.menu_open or time.monotonic() >= end:
                return
            time.sleep(0.03)

    def _publish_self_network(self) -> None:
        """Show the Pi's own IP + WiFi SSID while disconnected, so the user can
        see where the handset actually is (no SSH needed). Refreshed ~every 5 s."""
        now = time.monotonic()
        if now < self._next_netinfo:
            return
        self._next_netinfo = now + 5.0
        from . import discovery
        ip = discovery._local_ipv4() or ""
        self.shared.update(self_ip=ip, ssid=_wifi_ssid())

    # --- discovery ------------------------------------------------------

    def _discover(self) -> bool:
        """Resolve the mount IP (may block during a subnet sweep). Returns True
        if a target was found and assigned to the client."""
        force = self._fail_count >= REDISCOVER_AFTER
        if force:
            self.client.host = None  # don't keep retrying a stale address
        self.shared.update(searching=self.resolver.is_auto, connected=False)
        target = self.resolver.resolve(force=force)
        self._fail_count = 0
        self.shared.update(searching=False)
        if not target:
            return False
        self.client.host = target
        return True

    # --- action handling ------------------------------------------------

    def _drain_actions(self) -> None:
        while True:
            try:
                action = self.actions.get_nowait()
            except queue.Empty:
                return
            self._handle(action)

    def _handle(self, action: Action) -> None:
        kind = action.kind
        # KEY2 (MENU) toggles the settings menu in any mode.
        if kind == MENU:
            self._toggle_menu()
            return
        # While the menu is open, the controls navigate it instead of the mount.
        if self.menu_open:
            self._handle_menu(action)
            return
        if kind == MOVE:
            self._safe(lambda: self.client.send(protocol.move(action.arg)))
        elif kind == STOP:
            self._safe(lambda: self.client.send(protocol.stop(action.arg)))
        elif kind == STOP_ALL:
            self._safe(lambda: self.client.send(protocol.STOP_ALL))
        elif kind == RATE_DOWN:
            self._change_rate(-1)
        elif kind == RATE_UP:
            self._change_rate(+1)

    # --- settings menu --------------------------------------------------

    def _toggle_menu(self) -> None:
        self.menu_open = not self.menu_open
        self.menu_confirm = False
        if self.menu_open:
            # Stop any motion before the user fiddles with settings.
            self._safe(lambda: self.client.send(protocol.STOP_ALL))
            self.menu_index = 0
        self.shared.update(menu_open=self.menu_open, menu_index=self.menu_index,
                           menu_confirm=False)

    def _handle_menu(self, action: Action) -> None:
        """Navigate the settings menu. Joystick up/down selects a row, left/right
        (or KEY1/KEY3) changes a value -- or, for an action row (Park), right arms
        then runs it, left cancels. Centre press cancels a pending confirm, else
        closes the menu (KEY2 closes too, handled as MENU before we get here)."""
        kind = action.kind
        item = MENU_ITEMS[self.menu_index]
        if kind == MOVE and action.arg == "n":          # up
            self._menu_select(-1)
        elif kind == MOVE and action.arg == "s":        # down
            self._menu_select(+1)
        elif (kind == MOVE and action.arg == "w") or kind == RATE_DOWN:
            self._menu_change(item, -1)
        elif (kind == MOVE and action.arg == "e") or kind == RATE_UP:
            self._menu_change(item, +1)
        elif kind == STOP_ALL:                          # centre press
            if self.menu_confirm:
                self._set_confirm(False)                # cancel arm, stay in menu
            else:
                self._toggle_menu()
        # STOP (joystick release) is ignored in the menu.

    def _menu_select(self, delta: int) -> None:
        self.menu_index = (self.menu_index + delta) % len(MENU_ITEMS)
        self.menu_confirm = False
        self.shared.update(menu_index=self.menu_index, menu_confirm=False)

    def _menu_change(self, item: str, delta: int) -> None:
        if item in ACTION_ITEMS:
            if delta > 0:                               # right: arm, then run
                if self.menu_confirm:
                    self._run_action(item)
                else:
                    self._set_confirm(True)
            else:                                       # left: cancel arm
                self._set_confirm(False)
        elif item == "Tracking":
            self._cycle_tracking(delta)
        elif item == "Brightness":
            self._cycle_brightness(delta)

    def _set_confirm(self, value: bool) -> None:
        self.menu_confirm = value
        self.shared.update(menu_confirm=value)

    def _run_action(self, item: str) -> None:
        # Close the menu before acting.
        self.menu_open = False
        self.menu_confirm = False
        self.shared.update(menu_open=False, menu_confirm=False)
        if item == "Park":
            self._park_home()
        elif item == "Update":
            self._firmware_update()

    def _park_home(self) -> None:
        """Return the mount to its power-on (home) position and stop tracking.

        :hC# slews to home; OnStepX turns tracking off on arrival, and we send
        :Td# as well to be certain. Reflect tracking-off in the UI immediately."""
        self._safe(lambda: self.client.send(protocol.GOTO_HOME))    # :hC# no reply
        self._safe(lambda: self.client.query(protocol.track(False)))  # :Td# -> 1#
        if "off" in self.cfg.tracking_modes:
            self.tracking_index = self.cfg.tracking_modes.index("off")
            self.shared.update(tracking_mode=self._tracking_label())

    def _firmware_update(self) -> None:
        """Pull the latest handset software; on success exit so systemd relaunches
        it with the new code. Shows progress/result on the update screen."""
        from . import firmware
        self.shared.update(update_msg="Updating...")
        result = firmware.update()
        if result.ok and result.changed:
            if firmware.under_systemd():
                self.shared.update(update_msg="Updated.\nRestarting...")
                time.sleep(_UPDATE_RESTART_DELAY)
                self.stop_event.set()          # Restart=always relaunches us
                return
            self.shared.update(update_msg="Updated.\nRestart to apply")
        else:
            self.shared.update(update_msg=result.message)
        time.sleep(_UPDATE_RESULT_DELAY)
        self.shared.update(update_msg="")      # back to the status screen

    def _cycle_brightness(self, delta: int) -> None:
        n = len(self.cfg.brightness_levels)
        self.brightness_index = (self.brightness_index + delta) % n
        self.shared.update(brightness_index=self.brightness_index)
        save_settings({"brightness_index": self.brightness_index}, self._settings_path)

    def _clamp_brightness(self, idx) -> int:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return self.cfg.default_brightness_index
        return max(0, min(idx, len(self.cfg.brightness_levels) - 1))

    def _change_rate(self, delta: int) -> None:
        self.rate_index = max(0, min(self.rate_index + delta,
                                     len(self.cfg.slew_rates) - 1))
        self.shared.update(rate_index=self.rate_index, rate_label=self._rate_label())
        self._safe(lambda: self.client.send(protocol.rate(self._rate_code())))

    def _cycle_tracking(self, delta: int = 1) -> None:
        """Step the tracking mode by ``delta`` (wraps) and apply it on the mount."""
        self.tracking_index = (self.tracking_index + delta) % len(self.cfg.tracking_modes)
        mode = self.cfg.tracking_modes[self.tracking_index]
        # Tracking commands reply "1#"; use query() to consume the ack so it can't
        # corrupt the next status read. (A mode change may be several commands.)
        for cmd in protocol.tracking_commands(mode):
            self._safe(lambda c=cmd: self.client.query(c))
        self.shared.update(tracking_mode=self._tracking_label())

    def _rate_code(self) -> str:
        return self.cfg.slew_rates[self.rate_index]

    def _rate_label(self) -> str:
        return protocol.RATE_LABELS.get(self._rate_code(), self._rate_code())

    def _tracking_label(self) -> str:
        return protocol.tracking_label(self.cfg.tracking_modes[self.tracking_index])

    # --- polling --------------------------------------------------------

    def _poll_status(self) -> None:
        try:
            status = protocol.parse_status(self.client.query(protocol.GET_STATUS))
            ra = protocol.parse_ra(self.client.query(protocol.GET_RA))
            dec = protocol.parse_dec(self.client.query(protocol.GET_DEC))
        except (ConnectionError, ValueError) as exc:
            log.warning("poll failed: %s", exc)
            self.shared.update(connected=False)
            return
        self.shared.update(
            connected=True,
            ra=ra,
            dec=dec,
            tracking=status.tracking,
            slewing=status.slewing,
            parked=status.parked,
            at_home=status.at_home,
            error_code=status.error_code,
        )

    def _safe(self, fn) -> None:
        """Run a socket write, swallowing disconnects (the loop will reconnect)."""
        try:
            fn()
        except ConnectionError as exc:
            log.warning("command failed: %s", exc)
            self.shared.update(connected=False)


def _ui_loop(cfg: Config, shared: SharedState, stop_event: threading.Event) -> None:
    from .display import Display

    display = Display(dc=cfg.pins["lcd_dc"], rst=cfg.pins["lcd_rst"],
                      bl=cfg.pins["lcd_bl"], spi_hz=cfg.spi_hz, rotation=cfg.rotation,
                      brightness_levels=cfg.brightness_levels)
    frame_interval = 1.0 / cfg.ui_fps
    try:
        display.render(shared.snapshot(), force=True)
        while not stop_event.is_set():
            display.render(shared.snapshot())
            time.sleep(frame_interval)
    finally:
        display.close()


def run(cfg: Config, headless: bool = False) -> None:
    """Start the controller. ``headless`` skips GPIO + LCD (for dry-run testing)."""
    shared = SharedState(MountState(rate_index=cfg.default_rate_index))
    actions: "queue.Queue[Action]" = queue.Queue()
    stop_event = threading.Event()

    resolver = HostResolver(
        host=cfg.host, port=cfg.port, hostnames=cfg.discovery_hostnames,
        subnet_prefix=cfg.discovery_subnet_prefix,
        scan_timeout=cfg.discovery_scan_timeout, cache_path=HOST_CACHE,
        use_cache=cfg.discovery_cache,
    )
    # Client starts with no host; the worker resolves one (fixed IP or discovery)
    # before each connection attempt.
    client = OnStepClient(None, cfg.port, timeout=cfg.connect_timeout,
                          backoff_min=cfg.backoff_min, backoff_max=cfg.backoff_max)
    worker = CommsWorker(cfg, client, resolver, shared, actions, stop_event)
    comms_thread = threading.Thread(target=worker.run, name="comms", daemon=True)

    inputs = None
    if not headless:
        inputs = InputController(cfg.pins, actions)

    def _shutdown(*_a):
        log.info("shutting down")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    comms_thread.start()
    try:
        if headless:
            while not stop_event.is_set():
                st = shared.snapshot()
                log.info("conn=%s search=%s host=%s ra=%s dec=%s track=%s(%s) slew=%s rate=%s",
                         st.connected, st.searching, st.host or "-", st.ra, st.dec,
                         st.tracking_mode or "-", st.tracking, st.slewing, st.rate_label)
                time.sleep(1.0)
        else:
            _ui_loop(cfg, shared, stop_event)
    finally:
        stop_event.set()
        comms_thread.join(timeout=3.0)
        if inputs is not None:
            inputs.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OnStep WiFi hand controller")
    parser.add_argument("-c", "--config", default=None, help="path to config.yaml")
    parser.add_argument("--headless", action="store_true",
                        help="no GPIO/LCD; log status to console (dev/testing)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load(args.config)
    run(cfg, headless=args.headless)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
