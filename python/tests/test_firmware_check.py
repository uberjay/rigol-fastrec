"""Tests for the scope firmware version check."""

import pytest

from rigol_fastrec.exceptions import UnsupportedFirmware
from rigol_fastrec.firmware import check_idn, parse_idn

VALID = "RIGOL TECHNOLOGIES,MHO98,MHO9A27CM00217,00.01.00"


def test_parse_idn():
    vendor, model, serial, fw = parse_idn(VALID)
    assert vendor == "RIGOL TECHNOLOGIES"
    assert model == "MHO98"
    assert serial == "MHO9A27CM00217"
    assert fw == "00.01.00"


def test_supported_scope_passes():
    assert check_idn(VALID) == ("MHO98", "00.01.00")


def test_wrong_series_refused():
    with pytest.raises(UnsupportedFirmware):
        check_idn("RIGOL TECHNOLOGIES,DHO924,DHO9XX,00.01.00")


def test_unvalidated_firmware_refused():
    # Right series, unknown firmware version → fail closed.
    with pytest.raises(UnsupportedFirmware):
        check_idn("RIGOL TECHNOLOGIES,MHO98,MHO9A27CM00217,99.99.99")


def test_garbage_idn_refused():
    with pytest.raises(UnsupportedFirmware):
        check_idn("not a real idn")
