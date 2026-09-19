"""Strict reference parsing/comparison and export lifecycle without hardware."""
from dataclasses import replace
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from rigol_fastrec import Capture
from rigol_fastrec import csv_export as mod
from test_capture import acquire


def fixture_capture():
    rec, cap = acquire()
    arrays = {ch: (np.arange(4000).reshape(4, 1000)+30000+ch*100).astype(np.uint16)
              for ch in cap.samples}
    return rec, Capture(arrays, cap.metadata)


def csv_text(cap):
    pre = cap.metadata.acquisition.channel(1).preamble
    channels = list(cap.samples)
    header = ','.join(f'CH{ch}V' for ch in channels)+f',t0 ={pre.x_origin:e}, tInc = {pre.x_increment:e},\n'
    columns = np.column_stack([cap.to_volts(ch).ravel() for ch in channels])
    return header+''.join(','.join(f'{v:+.6e}' for v in row)+',,\n' for row in columns)


def reference(tmp_path, cap, text=None):
    path = tmp_path/'reference.csv'
    path.write_text(csv_text(cap) if text is None else text)
    return mod.parse_record_csv(path, frames=4, samples=1000)


def test_csv_reference_compares_all_samples_and_preserves_frame_order(tmp_path):
    _, cap = fixture_capture()
    ref = reference(tmp_path, cap)
    report = mod.compare_record_csv(cap, ref)
    assert sum(c['values'] for c in report['channels'].values()) == 8000
    assert report['t0_s'] == ref.t0 and report['increment_s'] == ref.increment
    ref.samples[1][:] = ref.samples[1][::-1]
    with pytest.raises(ValueError, match='samples exceed'):
        mod.compare_record_csv(cap, ref)


@pytest.mark.parametrize('change', ['screen', 'duplicate', 'truncated', 'extra', 'nan', 'column', 'interval'])
def test_malformed_or_wrong_source_csv_rejected(tmp_path, change):
    _, cap = fixture_capture()
    text = csv_text(cap)
    if change == 'screen': text = text.replace('CH1V,CH2V,', 'Time(s),CH1V,CH2V,', 1)
    if change == 'duplicate': text = text.replace('CH2V', 'CH1V', 1)
    if change == 'truncated': text = text[:text.rfind('\n', 0, -1)+1]
    if change == 'extra': text += text.splitlines()[1]+'\n'
    if change == 'nan': text = text.replace(text.splitlines()[1].split(',')[0], 'NaN', 1)
    if change == 'column': text = text.replace(',,\n', ',42,\n', 1)
    if change == 'interval': text = text.replace('tInc = 2.000000e-09', 'tInc = -1')
    with pytest.raises(ValueError):
        reference(tmp_path, cap, text)


@pytest.mark.parametrize('change', ['channel', 'shape', 'time', 'origin', 'tail', 'nonfinite', 'partial', 'packed'])
def test_mismatch_is_not_hidden_by_alignment_or_tail_trimming(tmp_path, change):
    _, cap = fixture_capture()
    ref = reference(tmp_path, cap)
    if change == 'channel': ref.samples.pop(2)
    if change == 'shape': ref.samples[1] = ref.samples[1][:-1]
    if change == 'time': ref = replace(ref, increment=ref.increment*2)
    if change == 'origin': ref = replace(ref, t0=ref.t0+ref.increment)
    if change == 'tail': ref.samples[1][-1, -1] += .01
    if change == 'nonfinite': ref.samples[1][-1, -1] = np.nan
    if change == 'partial': cap = Capture({c: a[:2] for c, a in cap.samples.items()}, replace(cap.metadata, read_frames=2))
    if change == 'packed': cap = Capture(cap.samples, replace(cap.metadata, transport='packed'))
    with pytest.raises(ValueError):
        mod.compare_record_csv(cap, ref)


@pytest.fixture
def exporter(monkeypatch, tmp_path):
    rec, cap = fixture_capture()
    rec.scpi.responses.update({':SAVE:STAT?': '1', ':SYSTem:ERRor?': '0,"No error"'})
    text = csv_text(cap)
    state = SimpleNamespace(stale=False, ambiguous=False, number=True, pull_fails=False,
                            truncate=False, never_started=False, calls=[], listings=0)
    monkeypatch.setattr(mod.shutil, 'which', lambda x: '/fake/adb')
    monkeypatch.setattr(mod.uuid, 'uuid4', lambda: SimpleNamespace(hex='a'*32))
    monkeypatch.setattr(mod.time, 'sleep', lambda _: None)
    ticks = count()
    monkeypatch.setattr(mod.time, 'monotonic', lambda: next(ticks)*.02)
    class Bridge:
        def __init__(self, *a): pass
        def files(self, paths):
            state.listings += 1
            if state.listings == 1 and not state.stale: return {}
            selected = paths[1] if state.number else paths[0]
            return {p: len(text) for p in paths} if state.ambiguous else {selected: len(text)}
        def call(self, *args, **kw):
            state.calls.append(args)
            if args[0] == 'get-state': return 'device'
            if args[0] == 'pull':
                if state.pull_fails: raise OSError('transfer failed')
                Path(args[2]).write_text(text[:-10] if state.truncate else text)
            return ''
    monkeypatch.setattr(mod, '_Adb', Bridge)
    rec.readback.record_csv_status.side_effect = lambda: dict(
        armed=state.never_started, redirected=int(not state.never_started),
        started=int(not state.never_started), finished=int(not state.never_started), expired=False)
    return rec, state, tmp_path/'export.csv'


@pytest.mark.parametrize('number', [True, False])
def test_complete_export_handles_auto_numbering_and_only_removes_own_file(exporter, number):
    rec, state, path = exporter
    state.number = number
    result = rec.export_csv(path, timeout=1)
    assert result.path == path and len(result.sha256) == 64
    assert result.writer_status['finished'] == 1
    assert result.scope_path.endswith(('0.csv' if number else 'a.csv'))
    assert state.calls[-1] == ('shell', 'rm -- '+result.scope_path)
    rec.readback.disarm_record_csv.assert_called_once()
    assert rec._record_metadata is not None


@pytest.mark.parametrize('failure', ['stale', 'ambiguous', 'pull_fails', 'truncate', 'never_started'])
def test_export_failure_disarms_and_never_deletes_reference_or_accepts_idle_status(exporter, failure):
    rec, state, path = exporter
    setattr(state, failure, True)
    with pytest.raises((ValueError, RuntimeError, OSError, TimeoutError)):
        rec.export_csv(path, timeout=1)
    assert not path.exists()
    assert not any(c[0] == 'shell' and c[1].startswith('rm ') for c in state.calls)
    assert rec._record_metadata is None
    if failure == 'stale':
        rec.readback.arm_record_csv.assert_not_called()
    else:
        rec.readback.disarm_record_csv.assert_called_once()


def test_export_does_not_overwrite_and_budget_precedes_instrument_writes(exporter):
    rec, state, path = exporter
    path.write_text('keep')
    with pytest.raises(FileExistsError): rec.export_csv(path)
    assert path.read_text() == 'keep'
    with pytest.raises(ValueError, match='budget'): rec.export_csv(path, max_values=1)
    assert not state.calls
    rec.readback.arm_record_csv.assert_not_called()
