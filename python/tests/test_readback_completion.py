"""Offline regressions for a socket reader that outlives its join timeout."""
from unittest.mock import Mock, call

import pytest

from rigol_fastrec import Readback, ReadbackShortRead
from rigol_fastrec import readback as readback_module


def test_unfinished_reader_closes_stream_and_never_returns_partial_arrays(monkeypatch):
    rb = Readback("unused")
    sock = Mock()
    rb._sock = sock
    rb._script = Mock()
    rb._script.exports_sync.read_frames.return_value = dict(ret=0, err=0)
    thread = Mock()
    thread.is_alive.return_value = True
    # Simulates a reader stalled in recv or a progress callback; no real thread
    # or socket is opened, and no 60-second wall-clock wait is needed.
    monkeypatch.setattr(readback_module.threading, "Thread", Mock(return_value=thread))

    with pytest.raises(ReadbackShortRead, match="stream reader did not finish"):
        rb.read(count=3, samples_per_frame=2)

    assert thread.join.call_args_list == [call(timeout=60.0), call(timeout=1.0)]
    sock.shutdown.assert_called_once()
    sock.close.assert_called_once()
    rb._script.exports_sync.readback_close.assert_called_once()
    assert rb._sock is None
    assert rb.last_read_stats is None
