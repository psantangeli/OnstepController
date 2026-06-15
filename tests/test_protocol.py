"""Off-Pi unit tests for the pure protocol layer. Run: python -m pytest tests/"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from onstep_handset import protocol as p


# --- command builders --------------------------------------------------------

@pytest.mark.parametrize("d,expected", [("n", ":Mn#"), ("s", ":Ms#"), ("e", ":Me#"), ("w", ":Mw#")])
def test_move(d, expected):
    assert p.move(d) == expected


@pytest.mark.parametrize("d,expected", [("n", ":Qn#"), ("s", ":Qs#"), ("e", ":Qe#"), ("w", ":Qw#")])
def test_stop(d, expected):
    assert p.stop(d) == expected


def test_bad_direction():
    with pytest.raises(ValueError):
        p.move("x")


def test_stop_all_constant():
    assert p.STOP_ALL == ":Q#"


def test_rate():
    assert p.rate("RC") == ":RC#"
    with pytest.raises(ValueError):
        p.rate("RX")


def test_track():
    assert p.track(True) == ":Te#"
    assert p.track(False) == ":Td#"


def test_tracking_commands():
    assert p.tracking_commands("off") == [":Td#"]
    assert p.tracking_commands("sidereal") == [":TQ#", ":Te#"]
    assert p.tracking_commands("solar") == [":TS#", ":Te#"]
    assert p.tracking_commands("lunar") == [":TL#", ":Te#"]
    assert p.tracking_commands("king") == [":TK#", ":Te#"]
    assert p.tracking_commands("SOLAR") == [":TS#", ":Te#"]  # case-insensitive
    with pytest.raises(ValueError):
        p.tracking_commands("warp")


def test_tracking_label():
    assert p.tracking_label("solar") == "Solar"
    assert p.tracking_label("off") == "Off"
    assert p.tracking_label("sidereal") == "Sidereal"


# --- status parsing ----------------------------------------------------------

def test_status_tracking_idle():
    # 'N' present (no goto), 'n' absent (tracking), 'p' not parked, trailing 0 = no error.
    st = p.parse_status("Np0#")
    assert st.tracking is True
    assert st.slewing is False
    assert st.parked is False
    assert st.has_error is False


def test_status_not_tracking():
    st = p.parse_status("nNp0#")
    assert st.tracking is False
    assert st.slewing is False


def test_status_slewing():
    # 'N' absent => goto/slew in progress.
    st = p.parse_status("np0#")
    assert st.slewing is True


def test_status_parked():
    st = p.parse_status("nNP0#")
    assert st.parked is True
    assert st.tracking is False


def test_status_at_home():
    st = p.parse_status("NpH0#")
    assert st.at_home is True


def test_status_error_code():
    st = p.parse_status("Np7#")
    assert st.error_code == "7"
    assert st.has_error is True


# --- coordinate parsing ------------------------------------------------------

def test_parse_ra():
    assert p.parse_ra("12:34:56#") == "12h 34m 56s"


def test_parse_dec_positive():
    assert p.parse_dec("+41*16:09#") == "+41° 16' 09\""


def test_parse_dec_negative():
    assert p.parse_dec("-05*30:00#") == "-05° 30' 00\""


def test_parse_dec_colon_separator():
    assert p.parse_dec("+41:16:09#") == "+41° 16' 09\""
