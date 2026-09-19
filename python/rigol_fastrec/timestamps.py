"""Per-frame acquisition counter values, independent of waveform sample axes."""
from __future__ import annotations

from dataclasses import dataclass
import operator

import numpy as np

from .exceptions import MetadataError


def validate_timestamp_request(timestamps: bool, average: int) -> None:
    """Validate before channel discovery, metadata queries or readback I/O."""
    if type(timestamps) is not bool:
        raise ValueError("timestamps must be boolean")
    if timestamps and average != 1:
        raise ValueError("timestamps and averaging are mutually exclusive (average must be 1)")


@dataclass(frozen=True)
class FrameTimestamps:
    """One uint64 acquisition-counter tick per frame, shared by all channels.

    ``first_frame`` is the zero-based record index of the first returned frame.
    Each tick is 250 ps. Subtract integer ticks before converting to seconds to
    preserve short intervals when the counter has a large value.
    """
    ticks: np.ndarray
    first_frame: int = 0
    tick_fs: int = 250000
    source: str = 'prefix-register'

    def __post_init__(self) -> None:
        ticks = np.asarray(self.ticks)
        if (ticks.dtype != np.dtype('uint64') or ticks.ndim != 1 or not ticks.size
                or np.any(ticks[1:] <= ticks[:-1])):
            raise MetadataError("timestamps must be a nonempty, increasing uint64 vector")
        if (type(self.first_frame) is not int or self.first_frame < 0
                or type(self.tick_fs) is not int or self.tick_fs != 250000
                or self.source != 'prefix-register'):
            raise MetadataError("invalid frame timestamp metadata")
        # Immutable buffer: a later read or a caller's input mutation cannot
        # change timestamps already attached to a Capture.
        frozen = np.frombuffer(ticks.tobytes(), dtype=np.uint64)
        object.__setattr__(self, 'ticks', frozen)

    def relative_seconds(self) -> np.ndarray:
        """Frame times relative to the first returned frame, in seconds."""
        return (self.ticks - self.ticks[0]).astype(np.float64) * (self.tick_fs * 1e-15)

    def intervals_seconds(self) -> np.ndarray:
        """Time between successive frames, in seconds (length N-1)."""
        return np.diff(self.ticks).astype(np.float64) * (self.tick_fs * 1e-15)

    def to_dict(self) -> dict:
        """Small metadata object; ticks are stored separately as uint64."""
        return dict(first_frame=self.first_frame, tick_fs=self.tick_fs, source=self.source)

    @classmethod
    def from_status(cls, status: dict, *, first: int, count: int) -> FrameTimestamps:
        """Decode exact decimal integers from the Frida RPC response."""
        try:
            if status['timestampBits'] != 64 or status['timestampFirstFrame'] != first:
                raise ValueError('wrong counter width or first frame')
            raw = status['frameTimestampTicks']
            if not isinstance(raw, list) or len(raw) != count:
                raise ValueError('timestamp count does not match frames')
            if any(not isinstance(t, str) or not t.isascii() or not t.isdecimal() for t in raw):
                raise ValueError('ticks must be decimal strings')
            values = [int(t) for t in raw]
            if any(t >= 1 << 64 for t in values):
                raise ValueError('timestamp outside uint64')
            return cls(np.array(values, dtype=np.uint64), operator.index(first),
                       status['timestampTickFs'], status['timestampSource'])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise MetadataError('invalid frame timestamp response') from exc
