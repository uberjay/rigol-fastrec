"""Check that the hardware harness itself does not turn failures into passes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[2]/'tools'
if not TOOLS.exists():
    pytest.skip('scope tools are checkout-only', allow_module_level=True)


def module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS/(name+'.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


harness = module('validate_scope')
captures = module('validate_captures')


def test_report_distinguishes_failures_skips_and_numpy_bools(tmp_path):
    v = harness.Validator(tmp_path)
    v.check('ok', np.bool_(True))
    v.run('broken', lambda: 1/0)
    v.skip('manual', 'needs independent export')
    assert v.summary() == 1
    report = json.loads((tmp_path/'report.json').read_text())
    assert (report['passed'], report['failed'], report['skipped']) == (1, 1, 1)
    assert [c['status'] for c in report['checks']] == ['PASS', 'FAIL', 'SKIP']


def test_afg_cleanup_runs_for_both_sources_when_body_raises():
    rec = SimpleNamespace(scpi=Mock())
    rec.scpi.query.return_value = '0'
    v = harness.Validator()
    with pytest.raises(RuntimeError):
        with harness.managed_afg(rec, v, [(':SOURce1',1,1e6), (':SOURce2',3,.7e6)]):
            raise RuntimeError('capture failed')
    assert [c.args[0] for c in rec.scpi.write.call_args_list] == [
        ':SOURce1:OUTPut:STATe OFF', ':SOURce2:OUTPut:STATe OFF']
    assert v.passed == 2


def test_failed_shutdown_still_attempts_second_source():
    rec = SimpleNamespace(scpi=Mock())
    rec.scpi.write.side_effect = [OSError('lost first command'), None]
    rec.scpi.query.return_value = '0'
    v = harness.Validator()
    with harness.managed_afg(rec, v, [(':SOURce1',1,1e6), (':SOURce2',3,.7e6)]):
        pass
    assert v.failed == 1 and v.passed == 1


def test_interrupt_is_reported_as_failure(tmp_path):
    v = harness.Validator(tmp_path)
    with harness.validation_session(v):
        raise KeyboardInterrupt()
    assert v.failed == 1
    assert 'KeyboardInterrupt' in (tmp_path/'report.json').read_text()


def test_sine_fit_recovers_amplitude_dc_and_rejects_wrong_time_scale():
    t = np.arange(10000)/50e6
    wave = .5*np.sin(2*np.pi*1e6*t+.3)+.2
    amp, dc, residual = captures.sine_fit(wave, 50e6, 1e6)
    assert amp == pytest.approx(1) and dc == pytest.approx(.2) and residual < 1e-10
    _, _, wrong = captures.sine_fit(wave, 100e6, 1e6)
    assert wrong > .3


def test_error_queue_failures_are_not_discarded():
    rec = SimpleNamespace(scpi=Mock())
    rec.scpi.query.side_effect = ['-113,"Undefined header"', '0,"No error"']
    assert captures.scpi_errors(rec) == ['-113,"Undefined header"']


def test_expected_error_does_not_accept_wrong_message_or_wrong_exception():
    def wrong():
        raise ValueError('unexpected cause')
    with pytest.raises(AssertionError):
        captures.expect_error(ValueError, wrong, 'different cause')
    with pytest.raises(ValueError):
        captures.expect_error(KeyError, wrong)


@pytest.mark.parametrize('initial,accept_reset', [(0., True), (1e-8, True), (1e-8, False)])
def test_deskew_reset_is_needed_and_verified(tmp_path, initial, accept_reset):
    deskew = [initial]
    scpi = Mock(enabled_channels=(1,))
    scpi.query.side_effect = lambda cmd: str(deskew[0]) if cmd.endswith('TCAL?') else '0,"No error"'
    def write(cmd):
        if cmd == ':CHAN1:TCAL 0' and accept_reset:
            deskew[0] = 0.
    scpi.write.side_effect = write
    rec = SimpleNamespace(scpi=scpi, configure=Mock())
    c = captures.CaptureChecks(rec, SimpleNamespace(trigger_source='CHAN2'),
                               harness.Validator(), [1], [], Mock(), tmp_path)
    if initial and not accept_reset:
        with pytest.raises(AssertionError, match='could not be reset'):
            c.configure(rate=50e6)
    else:
        c.configure(rate=50e6)
    resets = [call for call in scpi.write.call_args_list if call.args[0] == ':CHAN1:TCAL 0']
    assert len(resets) == bool(initial)
