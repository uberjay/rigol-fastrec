"""Offline SCPI tests: readiness, MDEP snapping and code→volts scaling."""

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pyvisa
import pytest

from rigol_fastrec.scpi import _RIGOL_VALID_MDEP, ScpiControl, snap_mdep
from rigol_fastrec import WaveformPreamble, ScalingError, ScopeRunTimeout
from rigol_fastrec import scpi as scpi_module


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
    s._preamble = {1: WaveformPreamble(1, 2, 1000, 1, 1e-9, -2e-7, 0, inc, orig, ref)}
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


def test_to_volts_unknown_channel_rejected():
    with pytest.raises(ScalingError):
        _scpi(1e-4, 0.0, 32768.0).to_volts(np.array([5, 7], dtype=np.uint16), 9)


def test_nonzero_y_origin_is_in_codes():
    np.testing.assert_allclose(_scpi(.001, 20., 32768.).to_volts(
        np.array([32788, 33788], dtype=np.uint16), 1), [0., 1.], atol=1e-6)


@pytest.fixture
def ready_scpi(monkeypatch):
    # No open(): all writes/queries go to a fake instrument, with virtual time.
    clock = SimpleNamespace(now=0.0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(scpi_module, "time", SimpleNamespace(
        monotonic=lambda: clock.now, sleep=sleep))
    control = ScpiControl("unused")
    instrument = Mock(timeout=7000)
    control._inst = instrument
    return control, instrument


@pytest.mark.parametrize("positions,progress", [
    (["1"], ["1"]),        # Both zeros can fall between queries.
    (["0", "0", "1"], ["1"]),
    (["4", "0", "1"], ["1"]),
    (["4", "4", "2", "1"], ["1"]),
    (["1", "0", "1"], ["0", "1"]),  # Stale 1, timer reset, then fresh 1.
    (["1", "1"], ["0", "1"]),       # Cursor zero was missed entirely.
    (["1", "1"], ["253000", "1"]),  # Previous record must not satisfy it.
])
def test_run_record_confirms_cursor_against_progress(ready_scpi, caplog,
                                                   positions, progress):
    control, instrument = ready_scpi
    expected = []
    counts = iter(progress)
    for position in positions:
        expected.append((":RECord:WREPlay:FCURrent?", position))
        if position == "1":
            expected.append((":RECord:WREPlay:FMAX?", next(counts)))
    responses = iter(expected)

    def query(command):
        expected_command, value = next(responses)
        assert command == expected_command
        assert instrument.timeout == 15000  # Widened for the whole readiness poll.
        return value

    instrument.query.side_effect = query
    with caplog.at_level("DEBUG", logger="rigol_fastrec.scpi"):
        control.run_record(253000, ready_timeout=10.0)

    assert instrument.query.call_count == len(expected)
    assert [call.args[0] for call in instrument.write.call_args_list] == [
        ":RECord:WRECord:OPERate STOP",
        ":RECord:WRECord:FRAMes 253000",
        ":RECord:WRECord:OPERate RUN",
    ]
    assert instrument.timeout == 7000
    if "4" in positions:
        assert "ignoring unexpected FCURrent=4" in caplog.text
    if "0" in progress:
        assert "FCURrent=1 but WREPlay FMAX=0" in caplog.text


@pytest.mark.parametrize("position,progress", [
    ("0", "1"), ("4", "1"), ("1", "0"), ("1", "253000"),
])
def test_wait_ready_times_out_without_confirmation(ready_scpi, position, progress):
    control, instrument = ready_scpi
    values = {":RECord:WREPlay:FCURrent?": position,
              ":RECord:WREPlay:FMAX?": progress}
    instrument.query.side_effect = values.__getitem__

    with pytest.raises(ScopeRunTimeout, match="readiness was not confirmed"):
        control.wait_ready(n_frames=253000, timeout=0.02)

    assert instrument.query.call_count > 1
    assert instrument.timeout == 7000


@pytest.mark.parametrize("error_query", ["cursor", "progress"])
def test_wait_ready_retries_visa_timeout(ready_scpi, error_query):
    control, instrument = ready_scpi
    error = pyvisa.errors.VisaIOError(pyvisa.constants.StatusCode.error_timeout)
    instrument.query.side_effect = ([error, "0", "1", "1"]
                                    if error_query == "cursor"
                                    else ["1", error, "1", "1"])

    control.wait_ready(n_frames=253000, timeout=1.0)

    assert instrument.query.call_count == 4
    assert instrument.timeout == 7000
