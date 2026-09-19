"""Timestamp investigation: SI units, integer precision, and fresh-frame binding."""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

TOOLS = Path(__file__).resolve().parents[2]/'tools'
if not TOOLS.exists():
    pytest.skip('scope tools are checkout-only', allow_module_level=True)

import tools.diagnostics.frame_timestamps as probe


@pytest.mark.parametrize('text,seconds', [
    ('3.5000s', '3.5'), ('109.48ms', '.10948'), ('73.000us', '.000073'),
    ('1.2500µs', '.00000125'), ('1.2500μs', '.00000125'),
    ('4.0000ns', '.000000004'), ('250ps', '.00000000025'),
])
def test_units(text, seconds):
    assert probe.parse_timestamp(text) == Decimal(seconds)


@pytest.mark.parametrize('text', ['', 'nan', '1.2', '1.2Hz', '3ms junk'])
def test_invalid_timestamp(text):
    with pytest.raises(ValueError):
        probe.parse_timestamp(text)


def test_intervals_subtract_integers_before_float_conversion():
    base = 10_000_000_000_000_000_000
    rows = [dict(elapsed_fs=str(base+i*250_000)) for i in range(3)]
    result = probe.period_summary(rows, 4e9)
    assert result['intervals_s'] == pytest.approx([250e-12, 250e-12])
    assert result['max_abs_period_error_ppm'] < 1e-6


def observer(snapshots, responses):
    ob = probe.FrameTimeObserver.__new__(probe.FrameTimeObserver)
    ob.rec = SimpleNamespace(scpi=Mock())
    ob.rec.scpi.query.side_effect = responses
    ob.snapshot = Mock(side_effect=snapshots)
    return ob


def test_selection_waits_for_new_update_for_requested_frame(monkeypatch):
    monkeypatch.setattr(probe.time, 'sleep', lambda _: None)
    value = dict(frame=2, state=3, elapsed_fs='7300000000000', first_tag='123')
    fresh = dict(sequence=8, **value)
    ob = observer([
        dict(sequence=7),
        dict(update=dict(sequence=7, **value)),  # same frame, stale timestamp
        dict(update=dict(sequence=8, **(value | dict(frame=3)))),  # wrong frame
        dict(update=fresh),
        dict(getter=value),
    ], ['2', '7.3000ms', '2'])
    row = ob.select(2)
    assert row['elapsed_fs'] == '7300000000000'
    assert ob.snapshot.call_count == 5


def test_selection_rejects_frame_change_during_query():
    value = dict(frame=2, state=3, elapsed_fs='7300000000000', first_tag='123')
    ob = observer([dict(sequence=0), dict(update=dict(sequence=1, **value)),
                   dict(getter=value)], ['2', '7.3000ms', '3'])
    with pytest.raises(RuntimeError, match='frame changed'):
        ob.select(2)


def test_selection_times_out_without_a_fresh_update(monkeypatch):
    ob = observer([dict(sequence=4), dict(update=None)], [])
    ticks = iter([0., 4.])
    monkeypatch.setattr(probe.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(TimeoutError, match='no fresh timestamp'):
        ob.select(2, timeout=3.)


def test_selection_rejects_cache_change_between_update_and_getter():
    value = dict(frame=2, state=3, elapsed_fs='7300000000000', first_tag='123')
    changed = value | dict(elapsed_fs='7400000000000')
    ob = observer([dict(sequence=0), dict(update=dict(sequence=1, **value)),
                   dict(getter=changed)], ['2', '7.3000ms', '2'])
    with pytest.raises(RuntimeError, match='frame changed'):
        ob.select(2)
