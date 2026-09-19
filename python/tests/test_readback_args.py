"""Readback encoding-argument validation — these raise up front, before any
scope I/O, so they're testable on an unopened Readback."""

import pytest

from rigol_fastrec.readback import Readback


def _rb():
    return Readback("unused")          # not open()ed


def _read(**kw):
    return _rb().read(count=kw.pop("count", 1), samples_per_frame=1000,
                      channels=[1], **kw)


def test_bad_sample_bits_raises():
    with pytest.raises(ValueError):
        _read(sample_bits=10)


def test_bad_transport_raises():
    with pytest.raises(ValueError):
        _read(transport="zip")


def test_8bit_packed_incompatible():
    with pytest.raises(ValueError):
        _read(sample_bits=8, transport="packed")


@pytest.mark.parametrize("kw", [{"sample_bits": 8}, {"transport": "packed"}])
def test_encoding_with_average_raises(kw):
    with pytest.raises(ValueError):
        _read(count=10, average=10, **kw)


def test_defaults_pass_validation_then_fail_on_not_open():
    # 16/raw with average>1 is valid → gets past validation to the open() check.
    from rigol_fastrec.exceptions import ScopeNotFound
    with pytest.raises(ScopeNotFound):
        _read(count=10, average=10)


def test_readback_stats_are_copied_and_invalidated_on_failed_request():
    rb = _rb()
    assert rb.last_read_stats is None
    rb._last_read_stats = {'chunk': 250}
    stats = rb.last_read_stats
    stats['chunk'] = 999
    assert rb.last_read_stats == {'chunk': 250}
    with pytest.raises(ValueError):
        rb.read(count=1, samples_per_frame=1000, sample_bits=10)
    assert rb.last_read_stats is None
