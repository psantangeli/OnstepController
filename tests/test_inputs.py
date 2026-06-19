"""Unit tests for the button -> Action mapping in InputController.

gpiozero isn't available off-Pi, so we inject a fake Button module and invoke
the wired callbacks directly.
"""

import os
import queue
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PINS = {name: i for i, name in enumerate(
    ["joy_up", "joy_down", "joy_left", "joy_right",
     "joy_press", "key1", "key2", "key3"])}


def _make_controller():
    fake = types.ModuleType("gpiozero")

    class FakeButton:
        def __init__(self, pin, pull_up=True, bounce_time=None):
            self.pin = pin
            self.when_pressed = None
            self.when_released = None

        def close(self):
            pass

    fake.Button = FakeButton
    sys.modules["gpiozero"] = fake

    from onstep_handset.inputs import InputController

    q: "queue.Queue" = queue.Queue()
    ic = InputController(_PINS, q)
    by_pin = {b.pin: b for b in ic._buttons}
    return ic, q, by_pin


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_key2_opens_menu():
    from onstep_handset.inputs import MENU
    _, q, btn = _make_controller()
    btn[_PINS["key2"]].when_pressed()
    assert [a.kind for a in _drain(q)] == [MENU]


def test_key1_key3_change_rate():
    from onstep_handset.inputs import RATE_DOWN, RATE_UP
    _, q, btn = _make_controller()
    btn[_PINS["key1"]].when_pressed()
    assert [a.kind for a in _drain(q)] == [RATE_DOWN]
    btn[_PINS["key3"]].when_pressed()
    assert [a.kind for a in _drain(q)] == [RATE_UP]


def test_joystick_move_stop_and_centre():
    from onstep_handset.inputs import MOVE, STOP, STOP_ALL
    _, q, btn = _make_controller()

    btn[_PINS["joy_up"]].when_pressed()
    a = _drain(q)
    assert a[0].kind == MOVE and a[0].arg == "n"
    btn[_PINS["joy_up"]].when_released()
    a = _drain(q)
    assert a[0].kind == STOP and a[0].arg == "n"

    btn[_PINS["joy_press"]].when_pressed()
    assert [a.kind for a in _drain(q)] == [STOP_ALL]
