"""Unit tests for the KEY1+KEY3 chord logic in InputController.

gpiozero isn't available off-Pi, so we inject a fake Button module and drive the
chord handlers directly.
"""

import os
import queue
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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

    pins = {name: i for i, name in enumerate(
        ["joy_up", "joy_down", "joy_left", "joy_right",
         "joy_press", "key1", "key2", "key3"])}
    q: "queue.Queue" = queue.Queue()
    return InputController(pins, q), q


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_chord_emits_menu_and_suppresses_rate():
    from onstep_handset.inputs import MENU
    ic, q = _make_controller()

    ic._chord_press("key1")()      # first key down -> nothing yet
    assert q.empty()
    ic._chord_press("key3")()      # both down -> MENU
    kinds = [a.kind for a in _drain(q)]
    assert kinds == [MENU]

    # Releasing either key after a chord must NOT emit a rate change.
    ic._chord_release("key1", "rate_down")()
    ic._chord_release("key3", "rate_up")()
    assert _drain(q) == []


def test_single_key_emits_rate_on_release():
    from onstep_handset.inputs import RATE_DOWN, RATE_UP
    ic, q = _make_controller()

    # KEY1 alone: nothing on press, RATE_DOWN on release.
    ic._chord_press("key1")()
    assert q.empty()
    ic._chord_release("key1", RATE_DOWN)()
    kinds = [a.kind for a in _drain(q)]
    assert kinds == [RATE_DOWN]

    # KEY3 alone -> RATE_UP on release.
    ic._chord_press("key3")()
    ic._chord_release("key3", RATE_UP)()
    kinds = [a.kind for a in _drain(q)]
    assert kinds == [RATE_UP]


def test_chord_state_resets_after_full_release():
    from onstep_handset.inputs import MENU, RATE_DOWN
    ic, q = _make_controller()

    # Chord, then fully release both.
    ic._chord_press("key1")()
    ic._chord_press("key3")()
    ic._chord_release("key1", "rate_down")()
    ic._chord_release("key3", "rate_up")()
    _drain(q)

    # A subsequent lone KEY1 press/release should behave normally again.
    ic._chord_press("key1")()
    ic._chord_release("key1", RATE_DOWN)()
    kinds = [a.kind for a in _drain(q)]
    assert kinds == [RATE_DOWN]
