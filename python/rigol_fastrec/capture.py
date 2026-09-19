"""Portable WaveRecord samples and metadata; no instrument connection required.

Time axes use the acquisition sample rate or saved SCPI preamble. Sample
indices include any readback crop.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

import numpy as np

from .config import Channel
from .timestamps import FrameTimestamps
from .exceptions import MetadataError, ScalingError


@dataclass(frozen=True)
class WaveformPreamble:
    format: int
    type: int
    points: int
    count: int
    x_increment: float
    x_origin: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float

    def __post_init__(self) -> None:
        values = asdict(self)
        if not all(math.isfinite(v) for v in values.values()):
            raise ScalingError("nonfinite waveform preamble")
        if self.format != 1 or self.type != 2:
            raise ScalingError("expected a WORD/RAW waveform preamble")
        if any(v != int(v) or v < 1 for v in (self.points, self.count)):
            raise ScalingError("preamble points/count must be positive integers")
        if self.x_increment <= 0 or self.y_increment <= 0:
            raise ScalingError("preamble increments must be positive")

    @classmethod
    def parse(cls, response: str) -> WaveformPreamble:
        try:
            fields = response.strip().split(',')
            if len(fields) != 10:
                raise ValueError("expected ten comma-separated fields")
            values = [float(v) for v in fields]
            if not all(math.isfinite(v) for v in values):
                raise ValueError("nonfinite field")
            if any(v != int(v) for v in values[:4]):
                raise ValueError("non-integer format/type/points/count")
            return cls(*(int(v) for v in values[:4]), *values[4:])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ScalingError(f"invalid waveform preamble: {response!r}") from exc

    def to_volts(self, codes):
        """Rigol WORD formula; attenuation is already included, not applied twice."""
        c = np.asarray(codes)
        factor = 256.0 if c.dtype == np.uint8 else 1.0
        return (c.astype(np.float32)*factor-self.y_reference-self.y_origin)*self.y_increment


@dataclass(frozen=True)
class ChannelMetadata:
    channel: int
    settings: Channel
    inverted: bool
    deskew_s: float
    units: str
    preamble: WaveformPreamble

    def __post_init__(self) -> None:
        s = self.settings
        if self.channel not in (1, 2, 3, 4) or self.units not in ('VOLT', 'AMP', 'WATT', 'UNKN'):
            raise MetadataError("invalid channel number or units")
        if not all(math.isfinite(v) for v in (s.range, s.offset, s.probe, self.deskew_s)):
            raise MetadataError("nonfinite channel settings")
        if s.range <= 0 or s.probe <= 0:
            raise MetadataError("channel range and probe ratio must be positive")


@dataclass(frozen=True)
class AcquisitionMetadata:
    idn: str
    model: str
    firmware: str
    sample_rate: float
    memory_depth: int
    acquisition_type: str
    timebase_scale_s: float
    timebase_offset_s: float
    trigger_source: str
    trigger_slope: str
    trigger_level: float
    trigger_sweep: str
    channels: tuple[ChannelMetadata, ...]

    def __post_init__(self) -> None:
        if (type(self.memory_depth) is not int or self.memory_depth < 1
                or not self.channels or len({c.channel for c in self.channels}) != len(self.channels)):
            raise MetadataError("invalid acquisition depth or channels")
        if not all(math.isfinite(v) for v in (self.sample_rate, self.timebase_scale_s,
                                             self.timebase_offset_s, self.trigger_level)):
            raise MetadataError("nonfinite acquisition settings")
        if self.sample_rate <= 0 or self.timebase_scale_s <= 0:
            raise MetadataError("sample rate and timebase scale must be positive")

    def channel(self, number: int) -> ChannelMetadata:
        for ch in self.channels:
            if ch.channel == number:
                return ch
        raise MetadataError(f"CHAN{number} is absent from acquisition metadata")

    def settings_key(self) -> dict:
        """Settings only: preamble origin may change with the selected segment."""
        state = asdict(self)
        for ch in state['channels']:
            ch.pop('preamble')
        return state


@dataclass(frozen=True)
class CaptureMetadata:
    acquisition: AcquisitionMetadata
    channels: tuple[int, ...]
    recorded_frames: int
    read_frames: int
    crop: tuple[int, int]
    average: int
    sample_bits: int
    transport: str
    requested_samples: int
    requested_sample_rate: float
    requested_trigger_offset_us: float
    host_ready_utc: str
    host_read_utc: str
    package_version: str
    agent_sha256: str
    schema_version: int = 1
    timing_basis: str = "SCPI RAW preamble"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> CaptureMetadata:
        try:
            v = dict(value)
            if v.get('schema_version') not in (1, 2):
                raise ValueError("unsupported capture schema")
            acq = dict(v['acquisition'])
            acq['channels'] = tuple(ChannelMetadata(
                **dict(ch, settings=Channel(**ch['settings']),
                       preamble=WaveformPreamble(**ch['preamble']))) for ch in acq['channels'])
            v['acquisition'] = AcquisitionMetadata(**acq)
            v['channels'] = tuple(v['channels'])
            v['crop'] = tuple(v['crop'])
            return cls(**v)
        except (KeyError, TypeError, ValueError) as exc:
            raise MetadataError("invalid capture metadata") from exc


@dataclass
class Capture:
    """Channel arrays plus their acquisition snapshot; independent of live state."""
    samples: dict[int, np.ndarray]
    metadata: CaptureMetadata
    timestamps: FrameTimestamps | None = None

    def __post_init__(self) -> None:
        m = self.metadata
        if m.schema_version not in (1, 2):
            raise MetadataError("unsupported capture schema")
        if (m.schema_version == 2) != (self.timestamps is not None):
            raise MetadataError("schema 2 requires frame timestamps; schema 1 has none")
        if self.timestamps is not None:
            if (m.average != 1 or self.timestamps.first_frame != 0
                    or len(self.timestamps.ticks) != m.read_frames):
                raise MetadataError("frame timestamps do not match capture frames/averaging")
        if (not m.channels or len(set(m.channels)) != len(m.channels)
                or set(self.samples) != set(m.channels)):
            raise MetadataError("sample channels do not match metadata")
        if any(type(v) is not int for v in (m.average, m.read_frames, m.recorded_frames, *m.crop, *m.channels)):
            raise MetadataError("frame, channel and sample indices must be integers")
        if (m.average < 1 or m.read_frames < 1 or m.read_frames > m.recorded_frames
                or m.read_frames % m.average):
            raise MetadataError("invalid frame/averaging counts")
        if len(m.crop) != 2 or not 0 <= m.crop[0] < m.crop[1] <= m.acquisition.memory_depth:
            raise MetadataError("invalid capture crop")
        if (m.sample_bits not in (8, 16) or m.transport not in ('raw', 'packed')
                or (m.sample_bits == 8 and m.transport != 'raw')
                or (m.average > 1 and (m.sample_bits != 16 or m.transport != 'raw'))):
            raise MetadataError("invalid capture encoding")
        rate = m.acquisition.sample_rate
        if not math.isfinite(rate) or rate <= 0:
            raise MetadataError("invalid actual sample rate")
        shape = (m.read_frames // m.average, m.crop[1]-m.crop[0])
        dtype = np.dtype('float32' if m.average > 1 else 'uint8' if m.sample_bits == 8 else 'uint16')
        for ch, data in self.samples.items():
            m.acquisition.channel(ch)
            if data.shape != shape or data.dtype != dtype or not np.isfinite(data).all():
                raise MetadataError(f"CHAN{ch} shape/dtype/data do not match metadata")

    def to_volts(self, channel: int):
        if channel not in self.samples:
            raise MetadataError(f"CHAN{channel} was not saved in this capture")
        ch = self.metadata.acquisition.channel(channel)
        if ch.units != 'VOLT':
            raise ScalingError(f"CHAN{channel} uses {ch.units}, not volts")
        return ch.preamble.to_volts(self.samples[channel])

    def time_axis(self, channel: int, *, reference: str = 'record_start'):
        """Sample times in seconds, shared by the frames and including crop.

        record_start uses queried ACQ:SRAT. scpi_preamble uses the saved RAW
        origin/reference and checks that its interval agrees with the queried
        sample rate.
        """
        acq = self.metadata.acquisition
        if channel not in self.samples:
            raise MetadataError(f"CHAN{channel} was not saved in this capture")
        pre = acq.channel(channel).preamble
        lo, hi = self.metadata.crop
        i = np.arange(lo, hi, dtype=np.float64)
        if reference == 'record_start':
            return i / acq.sample_rate
        if reference != 'scpi_preamble':
            raise ValueError("reference must be 'record_start' or 'scpi_preamble'")
        if not math.isclose(pre.x_increment*acq.sample_rate, 1., rel_tol=1e-5):
            raise MetadataError("SCPI preamble interval disagrees with acquisition sample rate")
        return (i-pre.x_reference)*pre.x_increment+pre.x_origin

    def save(self, path: str | Path) -> None:
        """Save raw arrays + UTF-8 JSON in one NPZ; never overwrite an existing file."""
        self.__post_init__()
        metadata = json.dumps(self.metadata.to_dict(), allow_nan=False)
        extra = {}
        if self.timestamps is not None:
            extra = dict(frame_timestamp_ticks=self.timestamps.ticks,
                         frame_timestamp_metadata=np.array(json.dumps(self.timestamps.to_dict())))
        # File object prevents numpy silently adding a second '.npz' suffix.
        with Path(path).open('xb') as f:
            np.savez(f, metadata=np.array(metadata),
                     **{f'ch{ch}': data for ch, data in self.samples.items()}, **extra)

    @classmethod
    def load(cls, path: str | Path) -> Capture:
        """Load without pickle or access to the scope; validate shape and encoding."""
        try:
            with np.load(path, allow_pickle=False) as archive:
                m = CaptureMetadata.from_dict(json.loads(str(archive['metadata'].item())))
                expected = {'metadata', *(f'ch{ch}' for ch in m.channels)}
                timestamps = None
                if m.schema_version == 2:
                    expected |= {'frame_timestamp_ticks', 'frame_timestamp_metadata'}
                    timestamps = FrameTimestamps(archive['frame_timestamp_ticks'],
                        **json.loads(str(archive['frame_timestamp_metadata'].item())))
                if set(archive.files) != expected:
                    raise MetadataError("unexpected/missing capture arrays")
                return cls({ch: archive[f'ch{ch}'] for ch in m.channels}, m, timestamps)
        except (KeyError, TypeError, ValueError) as exc:
            raise MetadataError("invalid capture archive") from exc
