"""Optional timestamps through host readback, metadata captures and NPZ files."""
from dataclasses import replace
import socket
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from rigol_fastrec import (AgentError, Capture, FrameTimestamps, MetadataError,
                          Readback, WaveRecorder)
from test_capture import acquire, recorder


def status(first=0, count=3):
    return dict(ret=0, err=0, timestampBits=64, timestampTickFs=250000,
                timestampFirstFrame=first, timestampSource='prefix-register',
                frameTimestampTicks=[str((1 << 60) + i) for i in range(first, first+count)])


def test_exact_counter_math_and_immutable_storage():
    raw = np.array([2**60, 2**60+1, 2**60+4], dtype=np.uint64)
    ts = FrameTimestamps(raw)
    raw[0] = 0
    assert ts.ticks[0] == 2**60
    np.testing.assert_allclose(ts.relative_seconds(), [0, 250e-12, 1e-9], rtol=1e-15)
    np.testing.assert_allclose(ts.intervals_seconds(), [250e-12, 750e-12], rtol=1e-15)
    with pytest.raises(ValueError):
        ts.ticks[0] = 0
    with pytest.raises(ValueError):
        ts.ticks.setflags(write=True)
    assert FrameTimestamps(np.array([5], dtype=np.uint64)).intervals_seconds().size == 0


@pytest.mark.parametrize('update', [
    {'frameTimestampTicks':['1','2']}, {'frameTimestampTicks':['1','2','2']},
    {'frameTimestampTicks':['-1','2','3']}, {'frameTimestampTicks':['1','2',str(2**64)]},
    {'frameTimestampTicks':[1,2,3]}, {'frameTimestampTicks':['1','2','3.0']},
    {'timestampFirstFrame':1}, {'timestampBits':48}, {'timestampTickFs':1},
    {'timestampSource':'unknown'},
])
def test_bad_agent_timestamp_response_rejected(update):
    with pytest.raises(MetadataError):
        FrameTimestamps.from_status(status() | update, first=0, count=3)


@pytest.mark.parametrize('kind', ['readback', 'read', 'read_capture'])
@pytest.mark.parametrize('options', [{'timestamps':True,'average':1000}, {'timestamps':1},
                                     {'timestamps':True,'average':0}])
def test_invalid_request_rejected_before_any_scope_access(kind, options):
    rb = Readback('unused')
    rb._last_frame_timestamps = FrameTimestamps(np.array([100], dtype=np.uint64))
    if kind == 'readback':
        call = lambda: rb.read(count=1000, samples_per_frame=1000, **options)
    else:
        rec = WaveRecorder('unused'); rec._rb = rb
        rec.channel_layout = Mock(side_effect=AssertionError('scope accessed'))
        rec._scpi.snapshot = Mock(side_effect=AssertionError('scope accessed'))
        call = lambda: getattr(rec, kind)(count=1000, **options)
    with pytest.raises(ValueError, match='mutually exclusive|boolean'):
        call()
    assert rb.last_frame_timestamps is None


def socket_readback():
    rb = Readback('unused')
    host, peer = socket.socketpair()
    host.settimeout(2); peer.settimeout(2)
    rb._sock = host
    seen = []
    corrupt = [False]
    def read_frames(args):
        seen.append(args)
        for i in range(args['count']):
            peer.sendall(struct.pack('<I2H', 2, i, i+1))
        if args.get('timestamps'):
            out = status(args['first'], args['count'])
            if corrupt[0]:
                out.pop('frameTimestampTicks')
            return out
        return dict(ret=0,err=0,chunk=250)
    rb._script = SimpleNamespace(exports_sync=SimpleNamespace(read_frames=read_frames))
    return rb, peer, seen, corrupt


def test_real_socket_read_preserves_samples_default_payload_and_subset_timestamps():
    rb, peer, seen, corrupt = socket_readback()
    try:
        options = dict(count=3, samples_per_frame=2, channels=[1], first=7)
        base = rb.read(**options)
        assert 'timestamps' not in seen[-1]
        assert rb.last_frame_timestamps is None
        got = rb.read(**options, timestamps=True)
        np.testing.assert_array_equal(base[1], got[1])
        assert seen[-1]['timestamps'] is True
        ts = rb.last_frame_timestamps
        assert ts.first_frame == 7 and len(ts.ticks) == 3
        assert 'frameTimestampTicks' not in rb.last_read_stats
        rb.read(**options)
        assert rb.last_frame_timestamps is None
        assert ts.ticks[0] == 2**60+7  # prior result survives later reads
        corrupt[0] = True
        with pytest.raises(AgentError, match='invalid frame timestamps'):
            rb.read(**options, timestamps=True)
        assert rb.last_frame_timestamps is None and rb.last_read_stats is None
        assert rb._sock is None
    finally:
        peer.close(); rb.close()


def test_capture_timestamps_persist_independently_and_archive_round_trip(tmp_path):
    rec = recorder(); rec.run(4,capture_metadata=True); rec.wait_recorded()
    ts = FrameTimestamps.from_status(status(count=4), first=0, count=4)
    rec._rb.last_frame_timestamps = ts
    cap = rec.read_capture(count=4, timestamps=True, crop=(2,9), transport='packed')
    assert rec._rb.read.call_args.kwargs['timestamps'] is True
    assert cap.timestamps is ts and cap.metadata.schema_version == 2
    rec.read(count=4, channels=[1])
    assert cap.timestamps is ts
    target = tmp_path/'timestamped.npz'; cap.save(target)
    loaded = Capture.load(target)
    assert loaded.metadata == cap.metadata
    np.testing.assert_array_equal(loaded.timestamps.ticks, ts.ticks)
    assert loaded.timestamps.to_dict() == ts.to_dict()
    np.testing.assert_array_equal(loaded.samples[1], cap.samples[1])
    with np.load(target,allow_pickle=False) as archive:
        assert archive['frame_timestamp_ticks'].dtype == np.dtype('uint64')
        saved = {k: archive[k] for k in archive.files}
    for key in ['frame_timestamp_ticks', 'frame_timestamp_metadata']:
        broken = tmp_path/(key+'.npz')
        np.savez(broken,**{k:v for k,v in saved.items() if k!=key})
        with pytest.raises(MetadataError):
            Capture.load(broken)


def test_default_capture_keeps_schema_one_and_no_timestamp_arrays(tmp_path):
    _, cap = acquire()
    assert cap.metadata.schema_version == 1 and cap.timestamps is None
    path = tmp_path/'old.npz'; cap.save(path)
    with np.load(path,allow_pickle=False) as f:
        assert set(f.files) == {'metadata','ch1','ch2'}
    assert Capture.load(path).timestamps is None


def test_capture_rejects_mismatched_timestamps_and_averaging():
    _, cap = acquire()
    ts = FrameTimestamps.from_status(status(count=4),first=0,count=4)
    with pytest.raises(MetadataError,match='schema'):
        Capture(cap.samples,cap.metadata,ts)
    for times in [None, replace(ts,first_frame=1), replace(ts,ticks=ts.ticks[:2])]:
        with pytest.raises(MetadataError):
            Capture(cap.samples,replace(cap.metadata,schema_version=2),times)
    _, avg = acquire(average=2)
    with pytest.raises(MetadataError,match='averaging'):
        Capture(avg.samples,replace(avg.metadata,schema_version=2),ts)


def test_failed_capture_metadata_check_clears_timestamp_result():
    rec = recorder(); rec.run(4,capture_metadata=True); rec.wait_recorded()
    original = rec.read
    def read(**kw):
        data = original(**kw)
        rec._rb._last_frame_timestamps = FrameTimestamps(np.array([1,2,3,4],dtype=np.uint64))
        rec._scpi.responses[':CHAN1:OFFS?'] = '1'
        return data
    rec.read = read
    with pytest.raises(MetadataError,match='settings changed'):
        rec.read_capture(count=4,timestamps=True)
    assert rec._rb._last_frame_timestamps is None
