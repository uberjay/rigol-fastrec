"""Checkout entry points, evidence provenance and diagnostic lifecycle checks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT/'tools').exists():
    pytest.skip('scope tools are checkout-only', allow_module_level=True)

from tools._support import bench, timestamps
from tools.diagnostics import frame_timestamps, replay_headers, stream_batches


@pytest.mark.parametrize('module', [
    'tools.validate_scope', 'tools.validate_timestamps',
    'tools.diagnostics.frame_timestamps', 'tools.diagnostics.register_timestamps',
    'tools.diagnostics.replay_headers', 'tools.diagnostics.stream_batches',
])
def test_command_help_from_checkout_without_scope(module):
    result = subprocess.run([sys.executable, '-m', module, '--help'], cwd=ROOT,
                            capture_output=True, text=True, timeout=20,
                            env={**os.environ, 'PYTHONPATH': str(ROOT/'python')})
    assert result.returncode == 0, result.stderr
    assert '--host' in result.stdout and '--output-dir' in result.stdout


def test_source_metadata_identifies_actual_bundle_and_adjacent_js(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, 'load_agent_source', lambda: 'test bundle')
    monkeypatch.setattr(bench.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='revision\n'))
    meta = bench.evidence_metadata(SimpleNamespace(host='scope', output_dir=tmp_path))
    assert meta['configuration']['output_dir'] == str(tmp_path)
    provenance = meta['provenance']
    assert provenance['revision'] == 'revision'
    assert provenance['agent_sha256'] == hashlib.sha256(b'test bundle').hexdigest()
    for name in ['diagnostics/frame_timestamps.js', 'diagnostics/replay_headers.js']:
        assert provenance['tool_sources_sha256'][name] == hashlib.sha256((ROOT/'tools'/name).read_bytes()).hexdigest()
    json.dumps(meta, allow_nan=False)


def test_display_observer_loads_adjacent_payload():
    rec = SimpleNamespace(scpi=SimpleNamespace(model='MHO98', firmware='00.01.00'),
                          readback=SimpleNamespace(_session=Mock()))
    observer = frame_timestamps.FrameTimeObserver(rec)
    payload = rec.readback._session.create_script.call_args.args[0]
    assert 'ApiRecord_UpdateTimeStamp' in payload and 'rpc.exports' in payload
    observer.close()
    rec.readback._session.create_script.return_value.unload.assert_called_once()


def test_header_probe_loads_adjacent_payload():
    rec = SimpleNamespace(readback=SimpleNamespace(_session=Mock()))
    probe = replay_headers.HeaderProbe(rec)
    payload = rec.readback._session.create_script.call_args.args[0]
    assert 'DevAnalyzeTrace_Read' in payload and 'rpc.exports' in payload
    probe.close()
    rec.readback._session.create_script.return_value.unload.assert_called_once()


def test_replay_defaults_to_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'argv', ['replay_headers', '--output-dir', str(tmp_path)])
    assert replay_headers.parse_args().study == 'prefix'


def test_register_reference_restores_original_script_when_read_fails():
    original = object()
    rb = SimpleNamespace(_script=original, read=Mock(side_effect=RuntimeError('read failed')))
    with pytest.raises(RuntimeError, match='read failed'):
        timestamps.register_reference(SimpleNamespace(readback=rb), 3, 1000, [1])
    assert rb._script is original


@pytest.mark.parametrize('failed', [False, True])
def test_stream_case_closes_generator_on_break_and_failure(monkeypatch, failed):
    closed = []
    def frames(**kwargs):
        try:
            if failed:
                raise TimeoutError('silent source')
            yield np.array([1, 2], dtype=np.uint16)
            raise AssertionError('should have stopped')
        finally:
            closed.append(True)
    times = iter([0., 2., 3.])
    monkeypatch.setattr(stream_batches.time, 'monotonic', lambda: next(times))
    result = stream_batches.run_case(SimpleNamespace(stream=frames), channel=1, batch=1,
                                    seconds=1., rate=1.)
    assert closed == [True]
    assert result['frames'] == (0 if failed else 1)
    assert bool(result['err']) is failed


@pytest.mark.parametrize('failed', [False, True])
def test_stream_command_records_results_and_returns_failure(monkeypatch, tmp_path, failed):
    rec = Mock()
    rec.__enter__ = Mock(return_value=rec)
    rec.__exit__ = Mock(return_value=False)
    rec.scpi.idn = 'RIGOL TECHNOLOGIES,MHO98,test,00.01.00'
    rec.scpi.query.return_value = '0'
    rec.channel_layout.return_value = SimpleNamespace(samples_per_frame=1000, stride=1)
    rec.max_frames.return_value = 1000
    monkeypatch.setattr(stream_batches, 'WaveRecorder', lambda **kwargs: rec)
    monkeypatch.setattr(stream_batches, 'afg_setup', lambda *a, **k: [])
    monkeypatch.setattr(stream_batches, 'hook_agent_stream_start', lambda: None)
    monkeypatch.setattr(stream_batches.time, 'sleep', lambda _: None)
    result = dict(frames=0 if failed else 5, elapsed=1., expected=5., dups=0,
                  bursts=[] if failed else [5], gaps=[], first_at=None if failed else .1,
                  err='stream failed' if failed else None)
    monkeypatch.setattr(stream_batches, 'run_case', lambda *a, **k: result)
    out = tmp_path/'run'
    monkeypatch.setattr(sys, 'argv', ['stream_batches', '--rates', '5', '--batches', '1',
                                    '--output-dir', str(out)])
    assert stream_batches.main() == (1 if failed else 0)
    report = json.loads((out/'report.json').read_text())
    assert report['cases'][0]['err'] == result['err']
    assert report['provenance']['agent_sha256']
    assert report['instrument'] == rec.scpi.idn
    assert report['checks'][-1]['name'] == ':SOURce1 output disabled on exit'
    assert report['checks'][-1]['status'] == 'PASS'
