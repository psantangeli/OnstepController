"""Status screen rendering on the Waveshare 1.3" ST7789 LCD via luma.lcd.

The panel is a true 240x240 with origin (0,0), so NO h/v offset is needed (the
y_offset=80 quirk applies only to the 240x135 variants). We draw with Pillow into
luma's canvas and only repaint when the state changes (full-frame pushes are
slow on the Pi Zero's ARMv6 core, so redraw-on-change keeps the UI responsive).
"""

from __future__ import annotations

import logging

from .state import MountState

log = logging.getLogger(__name__)

# The display is rendered MONOCHROME (grey on black) so it stays legible behind a
# red night-vision filter -- no hues. All text/lines render at a SINGLE intensity
# (scaled by the brightness setting); there is no dim/secondary level, so nothing
# is hard to read. State that used to be shown by dimness (inactive flags) is now
# shown by presence/absence instead.
_INK = 255
_BLACK = (0, 0, 0)

#: Settings-menu rows (extensible). Shared with main.py.
MENU_ITEMS = ["Tracking", "Brightness", "Park", "Update"]
#: Rows that perform an action (with confirm) rather than cycling a value.
ACTION_ITEMS = {"Park", "Update"}


def _grey(base: int, factor: float) -> tuple[int, int, int]:
    v = max(0, min(255, int(round(base * factor))))
    return (v, v, v)


class Display:
    """luma.lcd ST7789 wrapper with a single render(state) entry point."""

    def __init__(self, dc: int, rst: int, bl: int, spi_hz: int = 32_000_000,
                 rotation: int = 0, brightness_levels: list[float] | None = None) -> None:
        # Lazy imports so the module loads on a dev machine without luma/spidev.
        from luma.core.interface.serial import spi
        from luma.lcd.device import st7789

        self._brightness_levels = brightness_levels or [0.55, 0.78, 1.0]
        # Backlight (BL) is managed by the *device* (gpio_LIGHT), not the serial
        # bus. On the Waveshare 1.3" HAT BL is active-high, so active_low=False.
        serial = spi(port=0, device=0, gpio_DC=dc, gpio_RST=rst, bus_speed_hz=spi_hz)
        self._device = _make_st7789(st7789, serial, rotation, bl)
        try:
            self._device.backlight(True)
        except Exception:  # pragma: no cover - some builds auto-enable backlight
            pass
        self._fonts = _load_fonts()
        self._last_key: tuple | None = None
        log.info("display ready (st7789 240x240, rot=%d, mono)", rotation)

    def _factor(self, brightness_index: int) -> float:
        levels = self._brightness_levels
        if 0 <= brightness_index < len(levels):
            return levels[brightness_index]
        return levels[-1]

    def render(self, state: MountState, force: bool = False) -> None:
        """Repaint the screen if ``state`` changed (or ``force`` is set)."""
        key = _render_key(state)
        if not force and key == self._last_key:
            return
        self._last_key = key
        self._paint(state)

    def _paint(self, s: MountState) -> None:
        from luma.core.render import canvas

        factor = self._factor(s.brightness_index)
        ink = _grey(_INK, factor)   # the single text/line intensity

        with canvas(self._device) as draw:
            draw.rectangle(self._device.bounding_box, fill=_BLACK)
            if s.update_msg:
                self._paint_message(draw, "SOFTWARE UPDATE", s.update_msg, ink)
            elif s.menu_open:
                self._paint_menu(draw, s, ink)
            else:
                self._paint_status(draw, s, ink)

    def _paint_message(self, draw, title, message, ink) -> None:
        f_small, f_med, f_big, f_label = self._fonts
        draw.text((6, 4), title, font=f_med, fill=ink)
        draw.line((0, 30, 240, 30), fill=ink)
        y = 60
        for line in str(message).split("\n"):
            draw.text((6, y), line, font=f_med, fill=ink)
            y += 28

    def _paint_status(self, draw, s: MountState, ink) -> None:
        f_small, f_med, f_big, f_label = self._fonts

        # Title bar: connection / search / error -- conveyed by text, not colour.
        if s.searching:
            draw.text((6, 4), "SEARCHING...", font=f_med, fill=ink)
        elif not s.connected:
            draw.text((6, 4), "DISCONNECTED", font=f_med, fill=ink)
        elif s.has_error:
            draw.text((6, 4), f"ERROR {s.error_code}", font=f_med, fill=ink)
        else:
            draw.text((6, 4), "ONSTEP", font=f_med, fill=ink)
            if s.host:
                draw.text((104, 9), s.host, font=f_label, fill=ink)
        draw.line((0, 30, 240, 30), fill=ink)

        # While not connected, the RA/Dec area is blank -- use it to show where the
        # handset itself is on the network (so it can be found without SSH) and a
        # reminder that KEY2 still opens the menu.
        if not s.connected:
            draw.text((6, 42), "HANDSET IP", font=f_label, fill=ink)
            draw.text((6, 58), s.self_ip or "(no network)", font=f_med, fill=ink)
            draw.text((6, 96), "WIFI", font=f_label, fill=ink)
            draw.text((6, 112), s.ssid or "(unknown)", font=f_med, fill=ink)
            draw.line((0, 150, 240, 150), fill=ink)
            draw.text((6, 160), "KEY2 = settings menu", font=f_small, fill=ink)
            draw.text((6, 184), "(stops searching)", font=f_small, fill=ink)
            return

        # Coordinates (the headline data).
        draw.text((6, 42), "RA", font=f_label, fill=ink)
        draw.text((6, 58), s.ra, font=f_big, fill=ink)
        draw.text((6, 96), "DEC", font=f_label, fill=ink)
        draw.text((6, 112), s.dec, font=f_big, fill=ink)

        draw.line((0, 150, 240, 150), fill=ink)

        # Slew rate (left) and tracking mode (right).
        draw.text((6, 156), "RATE", font=f_label, fill=ink)
        draw.text((6, 170), s.rate_label or "--", font=f_med, fill=ink)

        draw.text((124, 156), "TRACK", font=f_label, fill=ink)
        draw.text((124, 170), s.tracking_mode or "--", font=f_med, fill=ink)

        # Status flags row: only the ACTIVE flags are shown (all at full ink).
        y = 212
        if s.slewing:
            draw.text((6, y), "SLEW", font=f_small, fill=ink)
        if s.at_home:
            draw.text((92, y), "HOME", font=f_small, fill=ink)
        if s.parked:
            draw.text((178, y), "PARK", font=f_small, fill=ink)

    def _paint_menu(self, draw, s: MountState, ink) -> None:
        f_small, f_med, f_big, f_label = self._fonts

        draw.text((6, 4), "SETTINGS", font=f_med, fill=ink)
        draw.line((0, 30, 240, 30), fill=ink)

        y = 50
        for i, item in enumerate(MENU_ITEMS):
            selected = (i == s.menu_index)
            marker = ">" if selected else " "   # selection shown by the marker
            draw.text((6, y), f"{marker} {item}", font=f_med, fill=ink)
            draw.text((150, y), self._menu_value(item, s, selected),
                      font=f_med, fill=ink)
            y += 28

        # Footer: controls hint.
        draw.line((0, 192, 240, 192), fill=ink)
        draw.text((6, 200), "Up/Dn select, L/R change", font=f_small, fill=ink)
        draw.text((6, 220), "KEY2 or center: exit", font=f_small, fill=ink)

    def _menu_value(self, item: str, s: MountState, selected: bool) -> str:
        if item == "Tracking":
            return s.tracking_mode or "--"
        if item == "Brightness":
            return self._brightness_bar(s.brightness_index)
        if item in ACTION_ITEMS:
            # Action row: ">" runs it; arms a confirm step first.
            return "Sure? >" if (selected and s.menu_confirm) else "Run >"
        return ""

    def _brightness_bar(self, index: int) -> str:
        n = len(self._brightness_levels)
        filled = max(1, min(index + 1, n))
        return "[" + "#" * filled + "-" * (n - filled) + "]"

    def close(self) -> None:
        try:
            self._device.backlight(False)
            self._device.cleanup()
        except Exception:  # pragma: no cover
            pass


def _make_st7789(st7789, serial, rotation: int, bl: int):
    """Construct the st7789 device, tolerating luma.lcd version differences.

    A true 240x240 ST7789 needs no offset (origin 0,0), so we never pass
    h_offset/v_offset -- some versions don't accept them and raise TypeError.
    We try to hand the backlight pin to luma (gpio_LIGHT + active-high), and
    progressively drop optional kwargs if a given build rejects them.
    """
    attempts = (
        dict(width=240, height=240, rotate=rotation, gpio_LIGHT=bl, active_low=False),
        dict(width=240, height=240, rotate=rotation, gpio_LIGHT=bl),
        dict(width=240, height=240, rotate=rotation),
    )
    last_error: TypeError | None = None
    for kwargs in attempts:
        try:
            return st7789(serial, **kwargs)
        except TypeError as exc:
            last_error = exc
            log.debug("st7789(%s) rejected: %s", sorted(kwargs), exc)
    raise last_error


def _load_fonts():
    from PIL import ImageFont

    def truetype(size: int):
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        ):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                # OSError = font file missing; ImportError = Pillow built without
                # freetype (libfreetype6 not installed). Fall through to default.
                continue
        # Fallback to the built-in bitmap font. Pillow >= 10.1 can scale it via a
        # size arg; older builds return a single fixed size.
        try:
            return ImageFont.load_default(size)
        except TypeError:
            return ImageFont.load_default()

    # small, medium, big (coords), label -- bumped up so the smaller text is
    # readable on the 240x240 panel (still fits; see _paint_* layouts).
    fonts = truetype(16), truetype(20), truetype(28), truetype(15)
    if any(getattr(f, "path", None) is None for f in fonts):
        log.warning("TrueType fonts unavailable (install libfreetype6 + "
                    "fonts-dejavu-core for larger text); using default font")
    return fonts


def _render_key(s: MountState) -> tuple:
    """Everything that affects the rendered pixels -- used to skip no-op repaints."""
    return (s.connected, s.searching, s.host, s.self_ip, s.ssid, s.ra, s.dec,
            s.tracking_mode, s.slewing, s.parked, s.at_home, s.error_code,
            s.rate_label, s.brightness_index, s.menu_open, s.menu_index,
            s.menu_confirm, s.update_msg)
