"""Thread-safe shared state between the comms thread and the UI loop.

The comms thread is the sole writer; the UI loop takes immutable snapshots.
A single Lock guards all fields -- contention is negligible at our poll rates.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class MountState:
    """Immutable snapshot of what the handset knows about the mount."""

    connected: bool = False
    searching: bool = False      # actively discovering the mount on the network
    host: str = ""               # resolved mount IP (for display)
    ra: str = "--h --m --s"
    dec: str = "--° --' --\""
    tracking: bool = False       # mount actually tracking (from :GU#)
    tracking_mode: str = ""      # selected mode label (Off/Sidereal/Solar/Lunar)
    slewing: bool = False
    parked: bool = False
    at_home: bool = False
    error_code: str = "0"
    rate_index: int = 0          # index into the configured slew_rates list
    rate_label: str = ""         # human label for the current rate
    # UI / settings menu
    brightness_index: int = 0    # index into config.brightness_levels
    menu_open: bool = False      # settings menu showing instead of status
    menu_index: int = 0          # selected row in the settings menu
    menu_confirm: bool = False   # an action row (e.g. Park) is armed for confirm
    update_msg: str = ""         # non-empty -> show the software-update screen

    @property
    def has_error(self) -> bool:
        return self.error_code not in ("", "0")


class SharedState:
    """Lock-guarded holder for the current MountState."""

    def __init__(self, initial: MountState | None = None) -> None:
        self._lock = threading.Lock()
        self._state = initial or MountState()

    def snapshot(self) -> MountState:
        """Return the current state (frozen dataclass -- safe to read freely)."""
        with self._lock:
            return self._state

    def update(self, **changes) -> MountState:
        """Apply field changes atomically and return the new state."""
        with self._lock:
            self._state = replace(self._state, **changes)
            return self._state
