"""Capture provenance, physical scaling and failure paths without a live scope."""
from dataclasses import replace
import json
from unittest.mock import Mock

import numpy as np
import pytest

from rigol_fastrec import (AcquisitionMetadata, Capture, CaptureMetadata, Channel,
                          ChannelMetadata, MetadataError, ScalingError,
                          Trigger, WaveformPreamble, WaveRecorder, ScpiControl)


PRE = '1,2,1000,1,2e-9,-4e-7,0,0.001,20,32768'


class FakeScpi(ScpiControl):
    def __init__(self):
        super().__init__('unused')
        self._inst = object()
        self.idn = 'RIGOL TECHNOLOGIES,MHO98,test,00.01.00'
        self.model, self.firmware = 'MHO98', '00.01.00'
        self.writes = []
        self.source = 1
        self.responses = {
            ':ACQ:MDEP?': '1000', ':ACQ:SRAT?': '5e8', ':ACQ:TYPE?': 'NORM',
            ':TIM:MAIN:SCAL?': '2e-7', ':TIM:MAIN:OFFS?': '6e-7',
            ':TRIG:EDGE:SOUR?': 'CHAN2', ':TRIG:EDGE:SLOP?': 'POS',
            ':TRIG:EDGE:LEV?': '1.5', ':TRIG:SWE?': 'NORM',
            ':RECord:WREPlay:FMAX?': '4',
        }
        for ch in range(1, 5):
            self.responses.update({f':CHAN{ch}:{key}?': value for key, value in {
                'DISP': '0', 'IMP': 'OMEG', 'SCAL': '.5', 'OFFS': '0', 'PROB': '10',
                'COUP': 'DC', 'BWL': 'OFF', 'INV': '0', 'TCAL': '0', 'UNIT': 'VOLT',
            }.items()})
        self.preambles = {ch: PRE for ch in range(1, 5)}
        self.bad_impedance = False

    def write(self, command):
        self.writes.append(command)
        if command.startswith(':WAV:SOUR '):
            self.source = int(command.removeprefix(':WAV:SOUR CHAN'))
        elif command.startswith(':CHAN'):
            key, value = command.split(' ', 1)
            if self.bad_impedance and key.endswith(':IMP'):
                return
            self.responses[key+'?'] = value

    def query(self, command):
        if command == ':WAV:PRE?':
            response = self.preambles[self.source]
        else:
            response = self.responses[command]
        if isinstance(response, Exception):
            raise response
        return response


def configured():
    scpi = FakeScpi()
    scpi.configure(samples=1000, sample_rate=1e9, trigger=Trigger(),
                   channels={1: Channel(probe=10, range=4)})
    return scpi


def recorder():
    rec = WaveRecorder('unused')
    rec._scpi = configured()
    rec._scpi.run_record = Mock()
    rec._scpi.wait_recorded = Mock()
    rec._mdep, rec._max_frames = 1000, 100
    rec._trigger_chan = 2
    rec._requested = dict(samples=1000, sample_rate=1e9, trigger_offset_us=-.4)
    rec._rb = Mock()
    rec._rb.channel_layout.return_value = dict(stride=2, enabledList=[1, 2])
    def read(**kw):
        lo, hi = kw['crop'] or (0, kw['samples_per_frame'])
        dtype = np.float32 if kw['average'] > 1 else np.uint8 if kw['sample_bits'] == 8 else np.uint16
        value = 128 if dtype is np.uint8 else 32788
        return {ch: np.full((kw['count']//kw['average'], hi-lo), value, dtype=dtype)
                for ch in kw['channels']}
    rec._rb.read.side_effect = read
    return rec


def acquire(**kw):
    rec = recorder()
    rec.run(4, capture_metadata=True)
    rec.wait_recorded()
    return rec, rec.read_capture(count=4, **kw)


@pytest.mark.parametrize('response', [
    '', '1,2,1000', PRE.replace('2e-9', 'NaN'), PRE.replace('0.001', '0'),
    PRE.replace('2e-9', '-1'), PRE.replace('1,2,', '0,2,'),
    PRE.replace('1,2,', '1,0,'), PRE.replace('1000', '3.5'), PRE+',extra',
])
def test_bad_preamble_rejected(response):
    with pytest.raises(ScalingError):
        WaveformPreamble.parse(response)


def test_failed_preamble_refresh_discards_all_old_scaling():
    s = configured()
    s.preambles[2] = TimeoutError('query failed')
    with pytest.raises(ScalingError, match='CHAN2'):
        s._cache_preamble((1, 2))
    assert s._preamble == {}
    with pytest.raises(ScalingError):
        s.to_volts([32788], 1)


def test_impedance_is_set_before_scale_including_implicit_trigger():
    s = FakeScpi()
    s.configure(samples=1000, sample_rate=1e9,
                trigger=Trigger(channel_impedance=50), channels={1: Channel()})
    assert s.writes.index(':CHAN1:IMP OMEG') < s.writes.index(':CHAN1:SCAL 0.0625')
    assert s.writes.index(':CHAN2:IMP FIFT') < s.writes.index(':CHAN2:SCAL 1')
    assert s.snapshot().channel(2).settings.impedance == 50


def test_failed_impedance_set_is_not_accepted():
    s = FakeScpi(); s.bad_impedance = True
    with pytest.raises(MetadataError, match='impedance readback'):
        s.configure(samples=1000, sample_rate=1e9,
                    trigger=Trigger(), channels={1: Channel(impedance=50)})
    assert not s._preamble


def test_actual_depth_and_rate_are_not_the_requested_values():
    s = FakeScpi()
    # Scope snaps/clamps independently of our request.
    assert s.configure(samples=1001, sample_rate=1e9, trigger=Trigger()) == 1000
    snap = s.snapshot()
    assert snap.memory_depth == 1000 and snap.sample_rate == 5e8
    assert snap.channel(2).preamble.x_origin == -4e-7


@pytest.mark.parametrize('key,value', [(':ACQ:SRAT?', 'nan'), (':ACQ:SRAT?', '0'),
                                       (':CHAN1:DISP?', '0'), (':ACQ:MDEP?', '10000')])
def test_bad_or_changed_snapshot_invalidates_scaling(key, value):
    s = configured(); s.responses[key] = value
    with pytest.raises(MetadataError):
        s.snapshot()
    assert not s._preamble


def test_archive_round_trip_preserves_codes_and_metadata(tmp_path):
    rec, cap = acquire(crop=(100, 110))
    # New API explicitly includes trigger, unlike legacy read().
    assert set(cap.samples) == {1, 2}
    assert cap.metadata.requested_sample_rate == 1e9
    assert cap.metadata.acquisition.sample_rate == 5e8
    np.testing.assert_allclose(cap.time_axis(1), np.arange(100, 110)*2e-9)
    np.testing.assert_allclose(cap.to_volts(1), 0., atol=1e-8)
    rec._scpi._preamble.clear()  # saved capture doesn't depend on mutable live state
    path = tmp_path/'capture.npz'; cap.save(path)
    loaded = Capture.load(path)
    assert loaded.metadata == cap.metadata
    for ch in cap.samples:
        np.testing.assert_array_equal(loaded.samples[ch], cap.samples[ch])
    with pytest.raises(FileExistsError):
        loaded.save(path)


def test_preamble_axis_preserves_crop_origin_reference_and_checks_interval():
    _, cap = acquire(crop=(100, 110))
    ch = cap.metadata.acquisition.channel(1)
    ch = replace(ch, preamble=replace(ch.preamble, x_reference=7))
    acq = replace(cap.metadata.acquisition, channels=(ch, cap.metadata.acquisition.channel(2)))
    cap.metadata = replace(cap.metadata, acquisition=acq)
    np.testing.assert_allclose(cap.time_axis(1, reference='scpi_preamble'),
                               (np.arange(100, 110)-7)*2e-9-4e-7)
    np.testing.assert_allclose(cap.time_axis(1), np.arange(100, 110)*2e-9)
    bad = replace(ch, preamble=replace(ch.preamble, x_increment=1e-9))
    acq = replace(cap.metadata.acquisition, channels=(bad, cap.metadata.acquisition.channel(2)))
    cap.metadata = replace(cap.metadata, acquisition=acq)
    with pytest.raises(MetadataError, match='disagrees'):
        cap.time_axis(1, reference='scpi_preamble')


@pytest.mark.parametrize('kwargs,dtype', [({}, np.uint16), ({'sample_bits': 8}, np.uint8),
                                         ({'transport': 'packed'}, np.uint16),
                                         ({'average': 2}, np.float32)])
def test_capture_encoding_is_preserved(kwargs, dtype, tmp_path):
    _, cap = acquire(channels=[1], **kwargs)
    cap.save(tmp_path/'capture.npz')
    loaded = Capture.load(tmp_path/'capture.npz')
    assert loaded.samples[1].dtype == dtype
    assert loaded.metadata.average == kwargs.get('average', 1)


def test_no_capture_metadata_without_opt_in_or_before_completion():
    rec = recorder(); rec.run(4)
    with pytest.raises(MetadataError):
        rec.read_capture(count=4)
    rec.run(4, capture_metadata=True)
    with pytest.raises(MetadataError):
        rec.read_capture(count=4)


def test_incomplete_record_is_rejected():
    rec = recorder(); rec.run(4, capture_metadata=True)
    rec._scpi.responses[':RECord:WREPlay:FMAX?'] = '3'
    with pytest.raises(MetadataError, match='frame count'):
        rec.wait_recorded()
    assert rec._record_metadata is None


def test_prearm_sample_rate_can_still_describe_previous_acquisition():
    rec = recorder(); rec.run(4, capture_metadata=True)
    rec._scpi.responses[':ACQ:SRAT?'] = '1e9'
    rec._scpi.preambles = {ch: PRE.replace('2e-9', '1e-9') for ch in range(1, 5)}
    rec.wait_recorded()
    cap = rec.read_capture(count=4)
    assert cap.metadata.acquisition.sample_rate == 1e9
    # Once the record is completed, even a rate-only change is rejected.
    rec._scpi.responses[':ACQ:SRAT?'] = '5e8'
    with pytest.raises(MetadataError, match='sample_rate'):
        rec.read_capture(count=4)


@pytest.mark.parametrize('phase', ['during_record', 'before_read', 'during_read'])
def test_setting_drift_rejected(phase):
    rec = recorder(); rec.run(4, capture_metadata=True)
    if phase == 'during_record':
        rec._scpi.responses[':CHAN1:OFFS?'] = '1'
        with pytest.raises(MetadataError, match='settings changed'):
            rec.wait_recorded()
    else:
        rec.wait_recorded()
        if phase == 'before_read':
            rec._scpi.responses[':CHAN1:OFFS?'] = '1'
        else:
            read = rec._rb.read.side_effect
            def change(**kw):
                data = read(**kw); rec._scpi.responses[':CHAN1:OFFS?'] = '1'; return data
            rec._rb.read.side_effect = change
        with pytest.raises(MetadataError, match='settings changed'):
            rec.read_capture(count=4)
    assert rec._record_metadata is None


@pytest.mark.parametrize('kwargs', [{'count': 5}, {'count': 3, 'average': 2},
                                   {'count': 4, 'crop': (1.5, 3)},
                                   {'count': 4, 'channels': [1, 1]}])
def test_invalid_capture_read_never_reaches_agent(kwargs):
    rec = recorder(); rec.run(4, capture_metadata=True); rec.wait_recorded()
    with pytest.raises(ValueError):
        rec.read_capture(**kwargs)
    rec._rb.read.assert_not_called()


def test_legacy_reads_keep_array_shape_and_default_channel_selection():
    rec = recorder()
    rec._scpi.snapshot = Mock(side_effect=AssertionError('legacy path queried metadata'))
    rec.run(4); rec.wait_recorded()
    data = rec.read(count=4)
    assert data.shape == (4, 1000) and isinstance(data, np.ndarray)
    assert rec._rb.read.call_args.kwargs['channels'] == [1]


def test_public_layout_matches_agent_physical_lanes():
    rec = recorder()
    rec._rb.channel_layout.return_value = dict(stride=4, enabledList=[1, 2, 4])
    assert rec.channel_layout().offsets == {1: 0, 2: 1, 4: 3}


@pytest.mark.parametrize('phase', ['before_read', 'during_read'])
def test_scaling_drift_rejected(phase):
    rec = recorder(); rec.run(4, capture_metadata=True); rec.wait_recorded()
    def drift():
        rec._scpi.preambles[1] = PRE.replace('0.001', '0.002')
    if phase == 'before_read':
        drift()
    else:
        read = rec._rb.read.side_effect
        def change(**kw):
            data = read(**kw); drift(); return data
        rec._rb.read.side_effect = change
    with pytest.raises(MetadataError, match='scaling changed'):
        rec.read_capture(count=4)
    assert rec._record_metadata is None


def test_only_saved_voltage_channels_can_be_converted():
    _, cap = acquire(channels=[1])
    with pytest.raises(MetadataError, match='not saved'):
        cap.to_volts(2)
    ch = cap.metadata.acquisition.channel(1)
    cap.metadata = replace(cap.metadata, acquisition=replace(
        cap.metadata.acquisition, channels=(replace(ch, units='AMP'),)))
    with pytest.raises(ScalingError, match='not volts'):
        cap.to_volts(1)


def test_archive_rejects_wrong_schema_and_missing_arrays(tmp_path):
    _, cap = acquire()
    doc = cap.metadata.to_dict(); doc['schema_version'] = 999
    p = tmp_path/'bad.npz'
    np.savez(p, metadata=np.array(json.dumps(doc)), ch1=cap.samples[1])
    with pytest.raises(MetadataError):
        Capture.load(p)
    np.savez(p, metadata=np.array(json.dumps(cap.metadata.to_dict())), ch1=cap.samples[1])
    with pytest.raises(MetadataError):
        Capture.load(p)
