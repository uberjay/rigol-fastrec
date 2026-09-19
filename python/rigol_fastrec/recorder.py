"""WaveRecorder facade — the primary host API.

Orchestrates the SCPI control plane (`ScpiControl`) and the frame readback
(`Readback`) for the common loop:

    configure() → run(N) → [app fires N triggers] → wait_recorded() → read()

`run()` and `read()` are deliberately separate so the application controls
trigger timing between them; the common case is the four calls above.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
import hashlib
import operator
from pathlib import Path

from .config import Channel, ChannelLayout, Trigger
from .capture import AcquisitionMetadata, Capture, CaptureMetadata
from .timestamps import validate_timestamp_request
from .exceptions import MetadataError
from .readback import Readback
from .scpi import ScpiControl, _chan_num

log = logging.getLogger("rigol_fastrec.recorder")


class WaveRecorder:
    def __init__(self, host: str, *, data_port: int = 5028,
                 frida_port: int = 27042) -> None:
        self._scpi = ScpiControl(host)
        self._rb = Readback(host, data_port=data_port,
                            frida_port=frida_port)
        self._mdep: int = 0          # snapped :ACQ:MDEP — PER CHANNEL
        self._trigger_chan: int | None = None
        self._max_frames: int = 0    # FMAX cached at configure() (depth-dependent)
        self._arm_metadata: AcquisitionMetadata | None = None
        self._record_metadata: AcquisitionMetadata | None = None
        self._record_frames = 0
        self._host_ready_utc = ''
        self._requested: dict = {}

    @property
    def scpi(self) -> ScpiControl:
        """The underlying SCPI control plane, for advanced use."""
        return self._scpi

    @property
    def readback(self) -> Readback:
        """The underlying readback client, for advanced use."""
        return self._rb

    def __enter__(self) -> "WaveRecorder":
        # SCPI firmware check (*IDN?) first, then Frida attach + agent check.
        self._scpi.open()
        self._rb.open(model=self._scpi.model, fw_version=self._scpi.firmware)
        return self

    def __exit__(self, *exc) -> None:
        self._invalidate_capture()
        self._scpi.stop_record()
        self._rb.close()
        self._scpi.close()

    # --- capture flow ------------------------------------------------------

    def configure(self, *, samples: int, sample_rate: float,
                  trigger: Trigger, trigger_offset_us: float = 0.0,
                  channels: dict[int, Channel] | None = None) -> None:
        """Set the scope state (vertical/trigger/timebase/MDEP) and enter
        WaveRecord mode. The trigger source channel is enabled implicitly;
        `samples` is snapped up to a valid Rigol MDEP."""
        self._invalidate_capture()
        self._mdep = 0
        self._max_frames = 0
        self._requested = {}
        self._mdep = self._scpi.configure(
            samples=samples, sample_rate=sample_rate, trigger=trigger,
            trigger_offset_us=trigger_offset_us, channels=channels)
        self._trigger_chan = _chan_num(trigger.source)
        self._scpi.begin_wave_record()
        # Probe + cache the max recordable frames at this depth (FMAX shrinks as
        # MDEP grows). run() validates against it; reads no triggers, just the cap.
        self._max_frames = self._scpi.probe_max_frames()
        self._requested = dict(samples=samples, sample_rate=sample_rate,
                               trigger_offset_us=trigger_offset_us)
        log.info("configure: %d samples/frame @ %g Sa/s, trigger %s %s @ %gV, "
                 "channels %s; max %d frames/record", self._mdep, sample_rate,
                 trigger.source, trigger.slope, trigger.level,
                 sorted(self._scpi.enabled_channels), self._max_frames)

    def channel_layout(self) -> ChannelLayout:
        """Live FPGA interleave layout: stride + per-channel offsets."""
        lay = self._rb.channel_layout()
        stride = int(lay["stride"])
        enabled = tuple(sorted(int(c) for c in lay["enabledList"]))
        # Match agent/native/layout.ts: 4-lane mode keeps physical gaps.
        offsets = {ch: ch-1 if stride >= 4 else i for i, ch in enumerate(enabled)}
        # MDEP is per channel, so samples_per_frame is the MDEP itself (the
        # engine interleaves stride channels into MDEP*stride, deinterleaved
        # back to MDEP per channel on readback).
        return ChannelLayout(stride=stride, enabled=enabled, offsets=offsets,
                             samples_per_frame=self._mdep)

    def max_frames(self, *, refresh: bool = False) -> int:
        """Max WaveRecord frames recordable at the configured depth. Cached by
        configure(); pass refresh=True to re-probe the scope."""
        if self._max_frames and not refresh:
            return self._max_frames
        self._max_frames = self._scpi.probe_max_frames()
        log.info("max_frames at this depth: %d", self._max_frames)
        return self._max_frames

    def run(self, n_frames: int, *, ready_timeout: float | None = None,
            capture_metadata: bool = False) -> None:
        """Run WaveRecord for `n_frames` and block until the producer pipeline
        is armed (:WREPlay:FCURrent reaches 1, before any trigger). Fire the DUT
        triggers AFTER this returns.

        Raises ValueError if `n_frames` exceeds the max recordable at this depth
        (one record can't hold them) — split across multiple run()/read() cycles
        instead. Spanning records needs the app to re-fire triggers per record,
        so it stays the caller's job."""
        self._invalidate_capture()
        if int(n_frames) != n_frames or n_frames < 1:
            raise ValueError("n_frames must be a positive integer")
        if self._max_frames and n_frames > self._max_frames:
            raise ValueError(
                f"requested {n_frames} frames exceeds the scope's max recordable "
                f"({self._max_frames}) at this depth ({self._mdep} samples/frame). "
                f"Split the capture across multiple run()/read() cycles, or "
                f"configure a shallower depth (fewer samples) so more frames fit.")
        before = self._scpi.snapshot() if capture_metadata else None
        if capture_metadata and not self._requested:
            raise MetadataError("configure before recording metadata")
        log.debug("run: arm %d frames", n_frames)
        self._scpi.run_record(n_frames, ready_timeout=ready_timeout)
        self._arm_metadata = before
        self._record_frames = int(n_frames)
        self._host_ready_utc = _utc_now()

    def wait_recorded(self, timeout: float = 10.0) -> None:
        """Block until the record completes, then transition to replay so the
        frames are readable."""
        log.debug("wait_recorded (timeout %.1fs)", timeout)
        self._record_metadata = None
        try:
            self._scpi.wait_recorded(timeout)
            if self._arm_metadata is not None:
                if self._scpi.recorded_frames() != self._record_frames:
                    raise MetadataError("completed frame count does not match the requested record")
                after = self._scpi.snapshot()
                # SRAT before a fresh acquisition can still describe the previous
                # record. Settings must match, but bind the rate to the completed
                # acquisition and require it to stay fixed throughout readback.
                self._check_settings(self._arm_metadata, after, allow_rate_refresh=True)
                self._record_metadata = after
        except Exception:
            self._invalidate_capture()
            raise

    def _invalidate_capture(self) -> None:
        self._arm_metadata = None
        self._record_metadata = None
        self._record_frames = 0

    @staticmethod
    def _check_settings(before: AcquisitionMetadata, after: AcquisitionMetadata,
                        *, check_scaling: bool = False,
                        allow_rate_refresh: bool = False) -> None:
        a_settings, b_settings = before.settings_key(), after.settings_key()
        if allow_rate_refresh:
            a_settings.pop('sample_rate')
            b_settings.pop('sample_rate')
        changed = [k for k in a_settings if a_settings[k] != b_settings[k]]
        if changed:
            raise MetadataError(f"scope settings changed during the record/read: {', '.join(changed)}")
        if check_scaling:
            # Origins may refer to different selected frames, but voltage scaling
            # must stay fixed for the completed record being read back.
            for ch in before.channels:
                a, b = ch.preamble, after.channel(ch.channel).preamble
                if (a.y_increment, a.y_origin, a.y_reference) != (b.y_increment, b.y_origin, b.y_reference):
                    raise MetadataError(f"CHAN{ch.channel} scaling changed during readback")

    def read(self, *, count: int, channels=None, channel: int | None = None,
             crop=None, average: int = 1,
             sample_bits: int = 16, transport: str = "raw", progress=None,
             timestamps: bool = False):
        """Read back `count` recorded frames.

        `channel=N` (or `channels=[N]`) returns a bare ndarray; `channels=[a,b]`
        returns a dict {ch: ndarray}. Default (neither given) reads the trace
        channels (all enabled minus the trigger source). `crop=(lo,hi)` is an
        in-agent per-channel sample window; `average=k` ships the float32 mean
        of each k-group. Output is uint16 codes (`average==1`) or float32 averages
        (`average>1`); use `to_volts()` to scale.

        For raw reads (`average==1`), two knobs trade wire bytes for resolution:
        `sample_bits` (16 → uint16, or 8 → uint8 from the top byte, −50%) and
        `transport` ("raw", or "packed" → the top 12 bits packed 2-per-3-bytes,
        −25%, still returned as uint16, drop-in with to_volts). `transport=
        "packed"` requires `sample_bits=16`; both apply only to raw reads
        (averaged reads are always float32 averages).

        ``timestamps=True`` stores per-frame counters in
        ``readback.last_frame_timestamps`` and requires ``average=1``. The
        waveform return shape is unchanged; defaults perform no timestamp pass.
        """
        self._rb._last_frame_timestamps = None
        self._rb._last_read_stats = None
        validate_timestamp_request(timestamps, average)
        if channel is not None and channels is not None:
            raise ValueError("pass either channel= or channels=, not both")

        if channel is not None:
            requested = [int(channel)]
        elif channels is not None:
            requested = [int(c) for c in channels]
        else:
            enabled = self.channel_layout().enabled
            requested = [c for c in enabled if c != self._trigger_chan] \
                or list(enabled)
        if not requested:
            raise ValueError("no channels to read")

        log.debug("read: count=%d channels=%s crop=%s average=%d "
                  "sample_bits=%d transport=%s",
                  count, requested, crop, average, sample_bits, transport)
        out = self._rb.read(
            count=count, samples_per_frame=self._mdep, channels=requested,
            crop=crop, average=average, sample_bits=sample_bits,
            transport=transport, progress=progress, timestamps=timestamps)
        return out[requested[0]] if len(requested) == 1 else out

    def read_capture(self, *, count: int, channels=None, crop=None,
                     average: int = 1, sample_bits: int = 16,
                     transport: str = 'raw', progress=None,
                     timestamps: bool = False) -> Capture:
        """Read arrays bound to metadata from run(capture_metadata=True).

        Call wait_recorded() first. Defaults to ALL enabled channels, including
        the trigger. Incomplete averaging groups are rejected. ``timestamps=True``
        attaches per-frame acquisition counters to ``Capture.timestamps`` and
        requires ``average=1``.
        """
        from . import __version__
        self._rb._last_frame_timestamps = None
        self._rb._last_read_stats = None
        validate_timestamp_request(timestamps, average)
        record = self._record_metadata
        if record is None:
            raise MetadataError("run(capture_metadata=True), then wait_recorded(), before read_capture()")
        if (int(count) != count or count < 1 or count > self._record_frames
                or int(average) != average or average < 1 or count % average):
            raise ValueError("count must fit the record and divide evenly by positive average")
        requested = list(channels) if channels is not None else [c.channel for c in record.channels]
        try:
            if any(isinstance(ch, bool) for ch in requested):
                raise TypeError("boolean channel")
            requested = [operator.index(ch) for ch in requested]
        except TypeError as exc:
            raise ValueError("channel numbers must be integers") from exc
        if not requested or len(set(requested)) != len(requested):
            raise ValueError("select distinct recorded channels")
        for ch in requested:
            record.channel(ch)
        window = tuple(crop) if crop is not None else (0, record.memory_depth)
        if (len(window) != 2 or any(int(v) != v for v in window)
                or not 0 <= window[0] < window[1] <= record.memory_depth):
            raise ValueError("crop must be an integer sample interval within the record")
        try:
            self._check_settings(record, self._scpi.snapshot(), check_scaling=True)
            arrays = self.read(count=int(count), channels=requested, crop=window,
                               average=int(average), sample_bits=sample_bits,
                               transport=transport, progress=progress, timestamps=timestamps)
            self._check_settings(record, self._scpi.snapshot(), check_scaling=True)
        except Exception:
            self._rb._last_frame_timestamps = None
            self._rb._last_read_stats = None
            self._invalidate_capture()
            raise
        if len(requested) == 1:
            arrays = {requested[0]: arrays}
        metadata = CaptureMetadata(
            acquisition=record, channels=tuple(requested), recorded_frames=self._record_frames,
            read_frames=int(count), crop=tuple(int(v) for v in window), average=int(average),
            sample_bits=sample_bits, transport=transport,
            requested_samples=self._requested['samples'],
            requested_sample_rate=self._requested['sample_rate'],
            requested_trigger_offset_us=self._requested['trigger_offset_us'],
            host_ready_utc=self._host_ready_utc, host_read_utc=_utc_now(),
            package_version=__version__,
            agent_sha256=hashlib.sha256(Path(__file__).with_name('_agent.js').read_bytes()).hexdigest(),
            schema_version=2 if timestamps else 1)
        return Capture(arrays, metadata, self._rb.last_frame_timestamps if timestamps else None)

    def export_csv(self, path, *, adb='adb', adb_serial=None, timeout=90.,
                   max_values=1_000_000):
        """Invoke Rigol's Record CSV writer and retrieve its file using ADB.

        Requires a completed metadata-bound record and exclusive scope use.
        Exports all frames/channels, refuses overwrite and bounds file size.
        See docs/CSV_EXPORT.md for prerequisites, side effects and limitations.
        """
        from .csv_export import export_record_csv
        return export_record_csv(self, path, adb=adb, adb_serial=adb_serial,
                                 timeout=timeout, max_values=max_values)

    def stream(self, *, channels=None, channel: int | None = None,
               crop=None, sample_bits: int = 16, transport: str = "raw",
               batch: int = 0):
        """Continuously stream frames, agent-driven. Yields one frame per
        recorded waveform until the caller stops iterating (break / close).

        The agent owns the capture loop and overlaps each batch's send with the
        next batch's capture, so this is true streaming, not a run()/read() loop.
        `channel=N` (or `channels=[N]`) yields bare ndarrays; `channels=[a,b]`
        yields `{ch: ndarray}` dicts. Defaults to the trace channels. Raw
        encodings only (`sample_bits` 16/8, `transport` raw/packed); no averaging.
        `batch` caps frames per FPGA capture (<=0 → the hardware max); the agent
        sizes each capture below that to the trigger rate, so sparse triggers
        arrive one at a time and fast ones in batches.

        configure() must have been called. The agent captures directly (it does
        not use run()/wait_recorded()), so feed triggers continuously (a
        repetitive / AUTO-triggered signal, or your DUT firing in a loop).
        """
        self._invalidate_capture()
        if channel is not None and channels is not None:
            raise ValueError("pass either channel= or channels=, not both")
        if channel is not None:
            requested = [int(channel)]
        elif channels is not None:
            requested = [int(c) for c in channels]
        else:
            enabled = self.channel_layout().enabled
            requested = [c for c in enabled if c != self._trigger_chan] \
                or list(enabled)
        if not requested:
            raise ValueError("no channels to stream")

        log.debug("stream: channels=%s crop=%s sample_bits=%d transport=%s batch=%d",
                  requested, crop, sample_bits, transport, batch)
        single = len(requested) == 1
        for frame in self._rb.stream(
                samples_per_frame=self._mdep, channels=requested, crop=crop,
                sample_bits=sample_bits, transport=transport, batch=batch):
            yield frame[requested[0]] if (single and isinstance(frame, dict)) else frame

    # --- volts conversion --------------------------------------------------

    def to_volts(self, codes, channel: int):
        """Scale raw codes (or float32 averages) from `read()` to volts."""
        return self._scpi.to_volts(codes, channel)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
