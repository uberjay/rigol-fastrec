"""SCPI control plane (pyvisa).

`ScpiControl` owns the scope's control channel: the ``*IDN?`` firmware check
(on ``open()``), the one-shot vertical/trigger/timebase ``configure()``, and
the WaveRecord lifecycle (run → ready → recorded → replay). The frame readback
is a separate channel (`Readback`); `WaveRecorder` orchestrates the two.

The WaveRecord sequence is order-sensitive: SCAL-before-MDEP, the signed
``trigger_offset_us`` window placement, the widened-VISA FMAX→1 readiness poll,
and the record→replay transition.
"""

from __future__ import annotations

import logging
import math
import time

from .config import Channel, Trigger
from .capture import AcquisitionMetadata, ChannelMetadata, WaveformPreamble
from .exceptions import MetadataError, ScalingError, ScopeRunTimeout
from .firmware import check_idn

# Every SCPI write/query is logged here at DEBUG — this is the "firehose" tier.
# The library only emits; the app turns it on via rigol_fastrec.enable_logging
# (INFO = high-level ops, DEBUG = + these raw commands). See logconf.py.
log = logging.getLogger("rigol_fastrec.scpi")


# :ACQ:MDEP accepts only this discrete set; arbitrary integers give "Data out
# of range". The top of the list (100M+) is reachable only with 1–2 channels
# enabled — enabling more re-snaps the cap downward.
_RIGOL_VALID_MDEP = (
    1_000, 10_000, 100_000, 1_000_000, 10_000_000,
    25_000_000, 50_000_000, 100_000_000, 250_000_000, 500_000_000,
)
_RIGOL_MAX_SAMPLE_RATE = 4e9        # MHO98 real-time sampling ceiling
_ANALOG_CHANNELS = (1, 2, 3, 4)     # MHO900 series is 4-channel


def snap_mdep(samples: int) -> int:
    """Round `samples` up to the smallest valid Rigol MDEP value."""
    for v in _RIGOL_VALID_MDEP:
        if v >= samples:
            return v
    raise ValueError(f"requested samples ({samples}) exceeds Rigol MDEP max "
                     f"({_RIGOL_VALID_MDEP[-1]})")


class ScpiControl:
    """Thin pyvisa wrapper around the scope's SCPI socket, with the host-side
    firmware check applied on open() and the WaveRecord control lifecycle."""

    def __init__(self, host: str, *, port: int = 5555,
                 timeout_ms: int = 7000) -> None:
        self._host = host
        self._port = port
        self._timeout_ms = timeout_ms
        self._rm = None
        self._inst = None
        self.model: str | None = None
        self.firmware: str | None = None
        self.idn: str | None = None
        # Set by configure(): actual MDEP per channel and enabled channel numbers.
        self.engine_samples: int = 0
        self.enabled_channels: tuple[int, ...] = ()
        self._preamble: dict[int, WaveformPreamble] = {}
        self._configured_channels: dict[int, Channel] = {}

    def open(self) -> "ScpiControl":
        import pyvisa
        self._rm = pyvisa.ResourceManager("@py")
        self._inst = self._rm.open_resource(
            f"TCPIP0::{self._host}::{self._port}::SOCKET")
        self._inst.timeout = self._timeout_ms
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"
        self.write("*CLS")
        # Check the firmware before doing anything else; raises UnsupportedFirmware.
        self.idn = self.query("*IDN?")
        self.model, self.firmware = check_idn(self.idn)
        return self

    def close(self) -> None:
        for obj in (self._inst, self._rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._inst = self._rm = None
        self._preamble.clear()
        self._configured_channels.clear()

    def write(self, cmd: str) -> None:
        """Issue a raw SCPI command (also the escape hatch for advanced/demo
        use). Logged at DEBUG to 'rigol_fastrec.scpi'."""
        assert self._inst is not None, "not open()ed"
        log.debug("→ %s", cmd)
        self._inst.write(cmd)

    def query(self, cmd: str) -> str:
        """Issue a raw SCPI query; return the stripped response. Logged at DEBUG
        with the round-trip time (handy for spotting slow queries)."""
        assert self._inst is not None, "not open()ed"
        t0 = time.monotonic()
        resp = self._inst.query(cmd).strip()
        log.debug("→ %s  ← %r  (%.1fms)", cmd, resp,
                  (time.monotonic() - t0) * 1000)
        return resp

    # --- WaveRecord control -----------------------------------------------

    def configure(self, *, samples: int, sample_rate: float,
                  trigger: Trigger, trigger_offset_us: float = 0.0,
                  channels: dict[int, Channel] | None = None) -> int:
        """Write the full scope state for a WaveRecord capture: per-channel
        vertical settings, the edge trigger (+ its source channel's vertical
        config), and the timebase/MDEP. Returns the snapped MDEP (engine
        samples). Enables the trigger source channel implicitly.

        Ordering matters: :TIM:MAIN:SCAL is set BEFORE :ACQ:MDEP — doing MDEP
        first makes the later SCAL recompute MDEP off the new timebase.
        """
        assert self._inst is not None, "not open()ed"
        self._preamble.clear()
        self._configured_channels.clear()
        self.engine_samples = 0
        self.enabled_channels = ()
        if not math.isfinite(sample_rate) or not 0 < sample_rate <= _RIGOL_MAX_SAMPLE_RATE:
            raise ValueError(
                f"sample_rate must be positive and <= {_RIGOL_MAX_SAMPLE_RATE:g} Sa/s")
        if not math.isfinite(trigger_offset_us):
            raise ValueError("trigger_offset_us must be finite")
        if samples < 1 or int(samples) != samples:
            raise ValueError("samples must be a positive integer")
        mdep = snap_mdep(int(samples))
        w = self.write

        chans = dict(channels or {})
        # Enable the trigger source channel implicitly so it counts toward the
        # interleave stride and the CMOS edge is visible. If the caller
        # didn't list it, synthesize its vertical config from the Trigger.
        trig_num = _chan_num(trigger.source)
        if trig_num is not None and trig_num not in chans:
            chans[trig_num] = Channel(range=trigger.channel_range,
                                      coupling=trigger.channel_coupling,
                                      probe=trigger.channel_probe,
                                      offset=trigger.channel_offset,
                                      impedance=trigger.channel_impedance)
        if not chans or any(n not in _ANALOG_CHANNELS for n in chans):
            raise ValueError("configure at least one analog channel in 1..4")

        w(":STOP")

        for num in sorted(chans):
            ch = chans[num]
            tag = f"CHAN{num}"
            w(f":{tag}:DISP ON")
            # Impedance changes the allowed vertical settings; apply it first.
            w(f":{tag}:IMP {'FIFT' if ch.impedance == 50 else 'OMEG'}")
            actual = _impedance(self.query(f":{tag}:IMP?"))
            if actual != ch.impedance:
                raise MetadataError(f"{tag} impedance readback {actual:g} != requested {ch.impedance:g} ohms")
            w(f":{tag}:COUP {ch.coupling}")
            w(f":{tag}:PROB {ch.probe}")           # set PROB first → volts at tip
            w(f":{tag}:SCAL {ch.range / 8.0:g}")   # 8 vertical divisions
            w(f":{tag}:OFFS {ch.offset:g}")
            w(f":{tag}:BWL {ch.bandwidth_limit}")

        # Disable every analog channel NOT in the configured set, so the enabled
        # count — which drives the FPGA interleave stride and thus the engine
        # frame size (mdep × stride) — is deterministic instead of inheriting
        # whatever was on before. Before :ACQ:MDEP so the depth snaps for the
        # final channel count.
        for num in _ANALOG_CHANNELS:
            if num not in chans:
                w(f":CHAN{num}:DISP OFF")

        # Timebase: window = samples / sample_rate; :TIM:MAIN:SCAL is per-div
        # (10 divs across the screen). :TIM:MAIN:OFFS is the screen-CENTER time;
        # the captured window's left edge = center − window/2, so to place the
        # left edge at trigger+offset → OFFS = window/2 + trigger_offset.
        window_s = mdep / float(sample_rate)
        w(f":TIM:MAIN:SCAL {window_s / 10.0:g}")
        w(f":TIM:MAIN:OFFS {window_s / 2.0 + trigger_offset_us * 1e-6:g}")
        w(f":ACQ:MDEP {mdep}")
        w(":ACQ:TYPE NORM")                        # not hardware-averaging

        # Edge trigger on the configured source.
        w(":TRIG:MODE EDGE")
        w(f":TRIG:EDGE:SOUR {trigger.source}")
        w(f":TRIG:EDGE:SLOP {trigger.slope}")
        w(f":TRIG:EDGE:LEV {trigger.level:g}")

        actual_mdep = self._positive_int(":ACQ:MDEP?")
        self.engine_samples = actual_mdep
        self.enabled_channels = tuple(sorted(chans))
        self._cache_preamble(self.enabled_channels)
        self._configured_channels = chans
        return actual_mdep

    def begin_wave_record(self, frame_interval: float = 1e-8) -> None:
        """Put the scope into WaveRecord mode (call once after configure()).
        NORM sweep so every trigger records a frame until FRAMes is reached."""
        assert self._inst is not None
        w = self.write
        w(":RUN")
        time.sleep(0.4)
        w(":TRIGger:SWEep NORM")
        w(":RECord:WRECord:ENABle 1")
        w(f":RECord:WRECord:FINTerval {frame_interval:g}")
        w(":RECord:WRECord:PROMpt 0")               # silence the per-run beep
        w(":RECord:WRECord:OPERate STOP")

    def probe_max_frames(self) -> int:
        """Max recordable frames at the current depth (:WREC:FMAX?). The scope
        only refreshes that value once WaveRecord has been RUN at the active
        MDEP, so run a throwaway 2-frame record to force the recompute, then
        stop. No triggers needed — it reads the cap, not a recorded count."""
        assert self._inst is not None
        w, q = self.write, self.query
        w(":RECord:WRECord:OPERate STOP")
        w(":RECord:WRECord:FRAMes 2")
        w(":RECord:WRECord:OPERate RUN")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if int(q(":RECord:WREPlay:FMAX?")) >= 1:
                break
            time.sleep(0.01)
        fmax = int(q(":RECord:WRECord:FMAX?"))
        w(":RECord:WRECord:OPERate STOP")
        return fmax

    def run_record(self, n_frames: int, *, wait_ready: bool = True,
                   ready_timeout: float | None = None) -> None:
        """Run WaveRecord for `n_frames` (:WRECord:OPERate RUN). With
        `wait_ready` (default), block until the producer pipeline is armed
        (:WREPlay:FCURrent reaches 1, before any trigger) — not just the trigger
        comparator set — so triggers fired afterward are caught."""
        assert self._inst is not None
        w = self.write
        w(":RECord:WRECord:OPERate STOP")
        w(f":RECord:WRECord:FRAMes {int(n_frames)}")
        w(":RECord:WRECord:OPERate RUN")
        if wait_ready:
            self.wait_ready(n_frames=n_frames, timeout=ready_timeout)

    def wait_ready(self, *, n_frames: int, timeout: float | None = None) -> None:
        """Block until the WaveRecord engine is armed and ready to catch
        triggers, by polling :WREPlay:FCURrent until it reaches 1. FCURrent
        becomes 1 when the engine comes up — BEFORE any trigger fires — so it's a
        true readiness signal (unlike FMAX, the capacity cap, which was the wrong
        one). NB FCURrent is a 1-based live *position*, not a frame count: it
        sits at 1 through the first trigger and only increments to 2 on the
        second, so don't read it as "frames recorded so far."

        Staging is erratically slow and the FIRST query can block while the scope
        comes up — longer than the default VISA read timeout (→ VI_ERROR_TMO
        before the deadline logic runs) — so widen the VISA read timeout for the
        poll, then restore it. Raises ScopeRunTimeout."""
        import pyvisa
        assert self._inst is not None
        if timeout is None:
            # Run staging is erratically slow and NOT frame-proportional — on a
            # healthy scope FCURrent→≥1 can take anywhere from ~1 s to ~16 s for
            # the same run (worse near memory-full). The deadline only bounds a
            # genuine failure — it returns the instant FCURrent reaches 1 — so be
            # generous. A real NORM+trigger capture comes up well within this.
            timeout = max(20.0, 8.0 + n_frames / 10000.0)
        raw = self._inst
        prev_timeout = raw.timeout
        raw.timeout = int(timeout * 1000) + 5000
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    if int(self.query(":RECord:WREPlay:FCURrent?")) >= 1:
                        return
                except pyvisa.errors.VisaIOError:
                    pass    # scope still staging the run — keep polling
                if time.monotonic() > deadline:
                    raise ScopeRunTimeout(
                        f"WaveRecord run timeout after {timeout:.0f}s "
                        f"({n_frames} frames): :WREPlay:FCURrent never reached 1, "
                        f"so the record engine never armed.")
                time.sleep(0.01)
        finally:
            raw.timeout = prev_timeout

    def wait_recorded(self, timeout: float = 10.0) -> None:
        """Block until the record completes (:WREC:OPER? → STOP), then
        transition to replay (:STOP; :WREPlay:OPER RUN) so the frames can be
        read back. Raises ScopeRunTimeout if the record never finishes."""
        assert self._inst is not None
        w, q = self.write, self.query
        deadline = time.monotonic() + timeout
        while True:
            if q(":RECord:WRECord:OPERate?").upper() == "STOP":
                break
            if time.monotonic() > deadline:
                fmax = q(":RECord:WREPlay:FMAX?")
                raise ScopeRunTimeout(
                    f"WaveRecord did not finish within {timeout:g}s "
                    f"(WREPlay FMAX={fmax}). Were enough triggers fired?")
            time.sleep(0.01)
        w(":STOP")
        w(":RECord:WREPlay:OPERate RUN")            # → replay; frames readable

    def stop_record(self) -> None:
        """Tear down WaveRecord/replay and return the scope to free-run. Safe
        to call from a finally/cleanup path (each command best-effort)."""
        if self._inst is None:
            return
        for cmd in (":RECord:WREPlay:OPERate STOP",
                    ":RECord:WRECord:OPERate STOP",
                    ":RECord:WRECord:ENABle 0",
                    ":RUN"):
            try:
                self.write(cmd)
            except Exception:
                pass

    # --- volts conversion --------------------------------------------------

    def _cache_preamble(self, channels: tuple[int, ...]) -> None:
        """Atomically cache full WORD/RAW preambles; never substitute identity.
        :WAV:PRE? → fmt,typ,pts,count,xinc,xorig,xref,yinc,yorig,yref."""
        assert self._inst is not None
        w, q = self.write, self.query
        self._preamble = {}
        w(":WAV:MODE RAW")
        w(":WAV:FORM WORD")
        pending = {}
        for ch in channels:
            try:
                w(f":WAV:SOUR CHAN{ch}")
                pending[ch] = WaveformPreamble.parse(q(":WAV:PRE?"))
            except Exception as e:
                raise ScalingError(f"CHAN{ch}: cannot obtain valid WORD/RAW scaling") from e
        self._preamble = pending

    def to_volts(self, codes, channel: int):
        """Convert codes to float32 volts for `channel`, using the cached WORD
        preamble. Vectorized over any array shape. Accepts uint16 codes (16-bit
        domain) or float32 averages directly; `uint8` codes (the top byte from
        `sample_bits=8`) are shifted back to the 16-bit domain (×256) first."""
        try:
            pre = self._preamble[channel]
        except KeyError as exc:
            raise ScalingError(f"no valid scaling for CHAN{channel}; configure first") from exc
        return pre.to_volts(codes)

    def _number(self, command: str, *, positive: bool = False) -> float:
        try:
            value = float(self.query(command))
            if not math.isfinite(value) or (positive and value <= 0):
                raise ValueError("invalid numeric response")
            return value
        except Exception as exc:
            raise MetadataError(f"invalid response to {command}") from exc

    def _positive_int(self, command: str) -> int:
        value = self._number(command, positive=True)
        if value != int(value):
            raise MetadataError(f"non-integer response to {command}")
        return int(value)

    def recorded_frames(self) -> int:
        """Query completed playback depth, not recording capacity or live position."""
        value = self._number(":RECord:WREPlay:FMAX?")
        if value < 0 or value != int(value):
            raise MetadataError("invalid completed frame count")
        return int(value)

    def snapshot(self) -> AcquisitionMetadata:
        """Query actual settings and preambles outside active recording.

        A failed snapshot invalidates cached scaling.
        """
        try:
            return self._snapshot()
        except Exception as exc:
            self._preamble.clear()
            if isinstance(exc, MetadataError):
                raise
            raise MetadataError("could not read acquisition metadata") from exc

    def _snapshot(self) -> AcquisitionMetadata:
        if not self._configured_channels or not self.idn:
            raise MetadataError("configure an open scope before taking a snapshot")
        q = self.query
        enabled = tuple(ch for ch in _ANALOG_CHANNELS if _bool(q(f":CHAN{ch}:DISP?")))
        if enabled != self.enabled_channels:
            raise MetadataError("enabled channels changed since configure()")
        depth = self._positive_int(":ACQ:MDEP?")
        if depth != self.engine_samples:
            raise MetadataError("memory depth changed since configure()")
        rate = self._number(":ACQ:SRAT?", positive=True)
        acq_type = q(":ACQ:TYPE?").upper()
        if acq_type != 'NORM':
            raise MetadataError("metadata captures currently require NORM acquisition")
        channels = []
        self._cache_preamble(enabled)
        for ch in enabled:
            tag = f":CHAN{ch}"
            settings = Channel(
                range=8*self._number(tag+":SCAL?", positive=True),
                offset=self._number(tag+":OFFS?"),
                probe=self._number(tag+":PROB?", positive=True),
                coupling=q(tag+":COUP?").upper(),
                bandwidth_limit=q(tag+":BWL?").upper(),
                impedance=_impedance(q(tag+":IMP?")))
            if settings.impedance != self._configured_channels[ch].impedance:
                raise MetadataError(f"CHAN{ch} impedance changed since configure()")
            channels.append(ChannelMetadata(
                channel=ch, settings=settings, inverted=_bool(q(tag+":INV?")),
                deskew_s=self._number(tag+":TCAL?"), units=q(tag+":UNIT?").upper(),
                preamble=self._preamble[ch]))
        return AcquisitionMetadata(
            idn=self.idn, model=self.model, firmware=self.firmware,
            sample_rate=rate, memory_depth=depth, acquisition_type=acq_type,
            timebase_scale_s=self._number(":TIM:MAIN:SCAL?", positive=True),
            timebase_offset_s=self._number(":TIM:MAIN:OFFS?"),
            trigger_source=q(":TRIG:EDGE:SOUR?").upper(),
            trigger_slope=q(":TRIG:EDGE:SLOP?").upper(),
            trigger_level=self._number(":TRIG:EDGE:LEV?"),
            trigger_sweep=q(":TRIG:SWE?").upper(), channels=tuple(channels))

    def __enter__(self) -> "ScpiControl":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.stop_record()
        self.close()


def _chan_num(source: str) -> int | None:
    """CHANn → n; None for non-vertical sources (EXT, D-lines, AC LINE…)."""
    s = source.upper().strip()
    if s.startswith("CHAN") and s[4:].isdigit():
        return int(s[4:])
    return None


def _impedance(response: str) -> float:
    value = response.strip().upper()
    if value == 'OMEG':
        return 1e6
    if value in ('FIFT', 'FIFTY'):
        return 50.
    raise MetadataError(f"unknown input impedance response: {response!r}")


def _bool(response: str) -> bool:
    value = response.strip().upper()
    if value in ('1', 'ON'):
        return True
    if value in ('0', 'OFF'):
        return False
    raise MetadataError(f"unknown boolean response: {response!r}")
