"""Pure SCPI-layer helpers: MDEP snapping and code→volts scaling."""

import numpy as np
import pytest

from rigol_fastrec.scpi import _RIGOL_VALID_MDEP, ScpiControl, snap_mdep


def test_snap_mdep_rounds_up_to_valid_depth():
    assert snap_mdep(1) == 1_000
    assert snap_mdep(1_000) == 1_000
    assert snap_mdep(1_001) == 10_000
    assert snap_mdep(25_000_000) == 25_000_000
    assert snap_mdep(_RIGOL_VALID_MDEP[-1]) == _RIGOL_VALID_MDEP[-1]


def test_snap_mdep_too_deep_raises():
    with pytest.raises(ValueError):
        snap_mdep(_RIGOL_VALID_MDEP[-1] + 1)


def _scpi(inc, orig, ref):
    s = ScpiControl("unused")              # not opened; to_volts only needs the preamble
    s._preamble = {1: (inc, orig, ref)}
    return s


def test_to_volts_uint16():
    v = _scpi(1e-4, 0.0, 32768.0).to_volts(
        np.array([32768, 42768], dtype=np.uint16), 1)
    assert v.dtype == np.float32
    np.testing.assert_allclose(v, [0.0, 1.0], atol=1e-6)


def test_to_volts_uint8_lifted_to_16bit_domain():
    # sample_bits=8 returns the top byte (code>>8); to_volts must ×256 it back.
    # 32768>>8 = 128 (midscale → 0 V), 42752>>8 = 167.
    v = _scpi(1e-4, 0.0, 32768.0).to_volts(np.array([128, 167], dtype=np.uint8), 1)
    assert abs(float(v[0])) < 1e-6
    np.testing.assert_allclose(v, [0.0, (167 * 256 - 32768) * 1e-4], atol=1e-6)


def test_to_volts_float32_means_passthrough():
    v = _scpi(1e-4, 0.0, 32768.0).to_volts(
        np.array([32768.0, 40000.0], dtype=np.float32), 1)
    np.testing.assert_allclose(v, [0.0, (40000 - 32768) * 1e-4], atol=1e-5)


def test_to_volts_unknown_channel_is_identity():
    # no preamble for ch 9 → (1.0, 0.0, 0.0): volts == codes
    v = _scpi(1e-4, 0.0, 32768.0).to_volts(np.array([5, 7], dtype=np.uint16), 9)
    np.testing.assert_allclose(v, [5.0, 7.0])
