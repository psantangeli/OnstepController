"""Map the Waveshare HAT's joystick + keys to controller actions.

Inputs are wired active-low with internal pull-ups, so gpiozero ``Button`` with
``pull_up=True`` is exactly right: pressed == pin low == ``when_pressed``.

To keep every socket write on the comms thread, callbacks here do NOT touch the
network. They push lightweight Action tuples onto a thread-safe queue that the
comms thread drains. Joystick directions emit a MOVE on press and a STOP on
release (hold-to-slew); the centre press and keys emit one-shot actions.

KEY2 opens/closes the settings menu (MENU). KEY1/KEY3 are slew-rate down/up.
Tracking-mode selection lives inside the settings menu, not on a button.
"""

from __future__ import annotations

import logging
import queue
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Action kinds placed on the queue.
MOVE = "move"          # arg = direction n/s/e/w
STOP = "stop"          # arg = direction n/s/e/w
STOP_ALL = "stop_all"  # arg = None
RATE_DOWN = "rate_down"
RATE_UP = "rate_up"
MENU = "menu"                 # KEY2: toggle the settings menu


@dataclass(frozen=True)
class Action:
    kind: str
    arg: str | None = None


# Joystick direction pins -> OnStep direction letter.
# UP=North, DOWN=South, LEFT=West, RIGHT=East (N/S=Dec, E/W=RA).
_JOY_DIRECTION = {
    "joy_up": "n",
    "joy_down": "s",
    "joy_left": "w",
    "joy_right": "e",
}


class InputController:
    """Owns the gpiozero Buttons and translates events into queued Actions."""

    def __init__(self, pins: dict[str, int], action_queue: "queue.Queue[Action]",
                 bounce_time: float = 0.03) -> None:
        # Imported lazily so the module loads on a dev machine without gpiozero.
        from gpiozero import Button

        self._queue = action_queue
        self._buttons = []

        def make(name: str, pull_up: bool = True):
            btn = Button(pins[name], pull_up=pull_up, bounce_time=bounce_time)
            self._buttons.append(btn)
            return btn

        # Joystick directions: hold-to-move, release-to-stop.
        for name, direction in _JOY_DIRECTION.items():
            btn = make(name)
            btn.when_pressed = self._emit(MOVE, direction)
            btn.when_released = self._emit(STOP, direction)

        # Centre press: emergency stop-all.
        make("joy_press").when_pressed = self._emit(STOP_ALL)

        # KEY1 / KEY2 / KEY3: rate down / settings menu / rate up.
        make("key1").when_pressed = self._emit(RATE_DOWN)
        make("key2").when_pressed = self._emit(MENU)
        make("key3").when_pressed = self._emit(RATE_UP)

        log.info("inputs ready (%d buttons)", len(self._buttons))

    def _emit(self, kind: str, arg: str | None = None):
        def handler() -> None:
            self._queue.put(Action(kind, arg))
        return handler

    def close(self) -> None:
        for btn in self._buttons:
            try:
                btn.close()
            except Exception:  # pragma: no cover - cleanup best effort
                pass
