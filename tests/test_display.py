"""Render smoke tests using luma's in-memory 'dummy' device (no hardware).

Skipped automatically when luma isn't installed (e.g. minimal CI). On the Pi and
in the dev venv (which has luma.lcd) these exercise the real _paint code paths.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("luma.core.render")

from luma.core.device import dummy  # noqa: E402

from onstep_handset import display as disp  # noqa: E402
from onstep_handset.state import MountState  # noqa: E402


def _display():
    """A Display wired to an in-memory dummy device (bypasses SPI/__init__)."""
    d = object.__new__(disp.Display)
    d._device = dummy(width=240, height=240, mode="RGB")
    d._brightness_levels = [0.35, 0.65, 1.0]
    d._fonts = disp._load_fonts()
    d._last_key = None
    return d


def _pixels(device):
    return list(device.image.convert("RGB").getdata())


def _luminance(device):
    return sum(sum(p) for p in _pixels(device))


def _is_monochrome(device):
    # Every pixel must be a pure grey (R == G == B) for the red-filter use case.
    return all(r == g == b for (r, g, b) in _pixels(device))


def test_status_screen_renders_monochrome():
    d = _display()
    st = MountState(connected=True, host="192.168.1.5", ra="12h 34m 56s",
                    dec="+41d 16' 09\"", rate_label="Center 8x",
                    tracking_mode="Solar", slewing=True, brightness_index=2)
    d.render(st, force=True)
    assert _luminance(d._device) > 0          # something was drawn
    assert _is_monochrome(d._device)          # no colour (red-filter safe)


def test_menu_screen_renders():
    d = _display()
    st = MountState(connected=True, menu_open=True, menu_index=0, brightness_index=1)
    d.render(st, force=True)
    assert _luminance(d._device) > 0
    assert _is_monochrome(d._device)


def test_brightness_changes_luminance():
    content = dict(connected=True, ra="12h 34m 56s", dec="+41d 16' 09\"",
                   rate_label="Center 8x", tracking_mode="Solar")
    dim = _display(); dim.render(MountState(brightness_index=0, **content), force=True)
    bright = _display(); bright.render(MountState(brightness_index=2, **content), force=True)
    assert _luminance(dim._device) < _luminance(bright._device)
