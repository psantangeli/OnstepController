"""Map the Waveshare HAT's joystick + keys to controller actions.

Inputs are wired active-low with internal pull-ups, so gpiozero ``Button`` with
``pull_up=True`` is exactly right: pressed == pin low == ``when_pressed``.

To keep every socket write on the comms thread, callbacks here do NOT touch the
network. They push lightweight Action tuples onto a thread-safe queue that the
comms thread drains. Joystick directions emit a MOVE on press and a STOP on
release (hold-to-slew); the centre press and keys emit one-shot actions.

KEY1 and KEY3 are special: pressed *together* they emit a single MENU action
(open/close the settings menu). To make that chord clean, their individual
rate-change actions fire on *release* and are suppressed if a chord fired while
either was held -- so opening the menu never also bumps the slew rate.
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
TRACK_CYCLE = "track_cycle"   # cycle tracking mode (off/sidereal/solar/lunar)
MENU = "menu"                 # KEY1+KEY3 chord: toggle the settings menu


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
        # KEY1+KEY3 chord tracking.
        self._chord_down: set[str] = set()
        self._chord_consumed = False

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

        # KEY2: cycle tracking mode.
        make("key2").when_pressed = self._emit(TRACK_CYCLE)

        # KEY1 / KEY3: rate down/up on release, or MENU when chorded together.
        make("key1").when_pressed = self._chord_press("key1")
        self._buttons[-1].when_released = self._chord_release("key1", RATE_DOWN)
        make("key3").when_pressed = self._chord_press("key3")
        self._buttons[-1].when_released = self._chord_release("key3", RATE_UP)

        log.info("inputs ready (%d buttons)", len(self._buttons))

    def _emit(self, kind: str, arg: str | None = None):
        def handler() -> None:
            self._queue.put(Action(kind, arg))
        return handler

    def _chord_press(self, name: str):
        def handler() -> None:
            self._chord_down.add(name)
            if {"key1", "key3"} <= self._chord_down:
                self._chord_consumed = True
                self._queue.put(Action(MENU))
        return handler

    def _chord_release(self, name: str, single: str):
        def handler() -> None:
            was_chord = self._chord_consumed
            self._chord_down.discard(name)
            if not self._chord_down:
                self._chord_consumed = False
            if not was_chord:
                self._queue.put(Action(single))
        return handler

    def close(self) -> None:
        for btn in self._buttons:
            try:
                btn.close()
            except Exception:  # pragma: no cover - cleanup best effort
                pass
