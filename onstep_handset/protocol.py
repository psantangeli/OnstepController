"""LX200 / OnStep command strings and response parsers.

This module is intentionally pure (no sockets, no hardware) so it can be unit
tested on any machine. Everything OnStep-specific about the wire format lives here.

References (verified):
  * OnStepX  src/telescope/mount/Mount.command.cpp
  * INDI     drivers/telescope/lx200_OnStep.cpp  (:GU# flag scanning)
  * OnStep command protocol wiki (onstep.groups.io)

Wire format: ASCII commands framed as ``:CC<params>#``. Responses are either
nothing (motion commands), a single ``0#``/``1#`` status, or a value terminated
by ``#``.  The ``:GU#`` status string is variable-length and its character
positions shift, so it MUST be scanned by character *presence*, never by offset.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Direction handling ------------------------------------------------------

# OnStep axis convention: n/s drive the Dec axis, e/w drive the RA axis.
DIRECTIONS = ("n", "s", "e", "w")


def move(direction: str) -> str:
    """Start a continuous slew in ``direction`` (one of n/s/e/w). No reply."""
    _check_direction(direction)
    return f":M{direction}#"


def stop(direction: str) -> str:
    """Halt motion on the axis for ``direction``. No reply."""
    _check_direction(direction)
    return f":Q{direction}#"


def _check_direction(direction: str) -> None:
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")


#: Halt all motion immediately (emergency stop). No reply.
STOP_ALL = ":Q#"


# --- Slew rate ---------------------------------------------------------------

# Valid LX200 rate codes accepted by OnStep, slowest -> fastest.
RATE_CODES = ("RG", "RC", "RM", "RS")
RATE_LABELS = {
    "RG": "Guide 1x",
    "RC": "Center 8x",
    "RM": "Find 20x",
    "RS": "Slew max",
}


def rate(code: str) -> str:
    """Select slew rate by LX200 rate code (e.g. ``RC``). Reply ignored."""
    if code not in RATE_CODES:
        raise ValueError(f"rate code must be one of {RATE_CODES}, got {code!r}")
    return f":{code}#"


# --- Tracking ----------------------------------------------------------------

def track(on: bool) -> str:
    """Enable (``:Te#``) or disable (``:Td#``) tracking. Replies ``1#``/``0#``."""
    return ":Te#" if on else ":Td#"


# Tracking rate selection (OnStepX). Reply ``1#``.
TRACK_ENABLE = ":Te#"
TRACK_DISABLE = ":Td#"
TRACK_SIDEREAL = ":TQ#"   # sidereal (stars)
TRACK_SOLAR = ":TS#"      # solar (the Sun)
TRACK_LUNAR = ":TL#"      # lunar (the Moon)
TRACK_KING = ":TK#"       # King rate (refraction-corrected sidereal)

#: Canonical mode order; config may use a subset/reorder.
TRACKING_MODES = ("off", "sidereal", "solar", "lunar", "king")
TRACKING_LABELS = {
    "off": "Off", "sidereal": "Sidereal", "solar": "Solar",
    "lunar": "Lunar", "king": "King",
}
_TRACKING_RATE_CMD = {
    "sidereal": TRACK_SIDEREAL, "solar": TRACK_SOLAR,
    "lunar": TRACK_LUNAR, "king": TRACK_KING,
}


def tracking_commands(mode: str) -> list[str]:
    """LX200 commands to select a tracking ``mode``.

    ``off`` disables tracking; any other mode sets that rate *and* enables
    tracking (so selecting e.g. Solar from Off starts tracking at solar rate).
    """
    mode = mode.lower()
    if mode == "off":
        return [TRACK_DISABLE]
    if mode not in _TRACKING_RATE_CMD:
        raise ValueError(f"unknown tracking mode {mode!r}; "
                         f"expected one of {TRACKING_MODES}")
    return [_TRACKING_RATE_CMD[mode], TRACK_ENABLE]


def tracking_label(mode: str) -> str:
    """Human label for a tracking mode (e.g. ``solar`` -> ``Solar``)."""
    return TRACKING_LABELS.get(mode.lower(), mode.capitalize())


# --- Status queries ----------------------------------------------------------

GET_STATUS = ":GU#"   # general status flag string
GET_RA = ":GR#"       # RA  -> "HH:MM:SS#"
GET_DEC = ":GD#"      # Dec -> "sDD*MM:SS#"
GET_PRODUCT = ":GVP#"  # product name -> "On-Step#" (both OnStep and OnStepX)
GET_VERSION = ":GVN#"  # firmware version -> e.g. "10.28n#" (OnStepX: "10." prefix)

# Home / "park to startup position". :hC# slews to the home position (the mount's
# power-on / counterweights-down reference) and OnStepX turns tracking off on
# arrival. No reply. We pair it with :Td# as belt-and-suspenders.
GOTO_HOME = ":hC#"


def is_onstep_product(reply: str) -> bool:
    """True if a ``:GVP#`` reply identifies an OnStep/OnStepX device.

    The product string is ``On-Step`` on the wire for both firmwares; we
    normalise (lowercase, drop hyphens) and look for ``onstep``.
    """
    return "onstep" in _strip(reply).lower().replace("-", "")


@dataclass
class Status:
    """Decoded ``:GU#`` flags relevant to the hand controller."""

    tracking: bool
    slewing: bool
    parked: bool
    at_home: bool
    error_code: str            # "0" == no error; non-zero == fault
    raw: str

    @property
    def has_error(self) -> bool:
        return self.error_code not in ("", "0")


def parse_status(reply: str) -> Status:
    """Parse a ``:GU#`` reply.

    The flag string is position-independent, so we test for the presence of
    individual characters (matching INDI's strstr-style scan):

      * ``n``  -> NOT tracking            (absent => tracking)
      * ``N``  -> NO goto in progress     (absent => slewing)
      * ``P``  -> parked                  (``p`` => not parked)
      * ``H``  -> at home position
      * trailing digit -> general error code (``0`` == none)
    """
    s = _strip(reply)
    tracking = "n" not in s
    slewing = "N" not in s
    parked = "P" in s
    at_home = "H" in s

    # The last character is the general error code when it is a digit.
    error_code = s[-1] if s and s[-1].isdigit() else "0"

    return Status(
        tracking=tracking,
        slewing=slewing,
        parked=parked,
        at_home=at_home,
        error_code=error_code,
        raw=s,
    )


def parse_ra(reply: str) -> str:
    """Normalise an RA reply (``HH:MM:SS#``) to a display string ``HHh MMm SSs``."""
    s = _strip(reply)
    h, m, sec = _split_sexagesimal(s)
    return f"{h:02d}h {m:02d}m {sec:02d}s"


def parse_dec(reply: str) -> str:
    """Normalise a Dec reply (``sDD*MM:SS#``) to a display string ``+DD° MM' SS"``.

    OnStep uses ``*`` as the degree separator in high precision and may use ``:``;
    both are handled.
    """
    s = _strip(reply).replace("*", ":").replace("\xdf", ":")
    sign = "+"
    if s and s[0] in "+-":
        sign = s[0]
        s = s[1:]
    d, m, sec = _split_sexagesimal(s)
    return f"{sign}{d:02d}° {m:02d}' {sec:02d}\""


# --- helpers -----------------------------------------------------------------

def _strip(reply: str) -> str:
    """Remove the trailing ``#`` terminator and surrounding whitespace."""
    return reply.strip().rstrip("#")


def _split_sexagesimal(s: str) -> tuple[int, int, int]:
    """Split ``A:B:S`` (or ``A:B`` with no seconds) into three ints."""
    parts = s.split(":")
    nums = [int(p) for p in parts if p != ""]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]
