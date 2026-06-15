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
import sys
import time

from .config import Channel, Trigger
from .exceptions import ScopeRunTimeout
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
        # Set by configure(): the snapped MDEP (engine samples, all channels
        # interleaved) and the ascending list of enabled channel numbers.
        self.engine_samples: int = 0
        self.enabled_channels: tuple[int, ...] = ()
        # Per-channel WORD-format scaling, for to_volts(): {ch: (inc, orig, ref)}.
        self._preamble: dict[int, tuple[float, float, float]] = {}

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
        self.model, self.firmware = check_idn(self.query("*IDN?"))
        return self

    def close(self) -> None:
        for obj in (self._inst, self._rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._inst = self._rm = None

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
        if float(sample_rate) > _RIGOL_MAX_SAMPLE_RATE:
            raise ValueError(
                f"sample_rate {sample_rate:g} exceeds the MHO98 max real-time "
                f"rate ({_RIGOL_MAX_SAMPLE_RATE:g} Sa/s)")
        mdep = snap_mdep(int(samples))
        w = self.write

        w(":STOP")

        chans = dict(channels or {})
        # Enable the trigger source channel implicitly so it counts toward the
        # interleave stride and the CMOS edge is visible. If the caller
        # didn't list it, synthesize its vertical config from the Trigger.
        trig_num = _chan_num(trigger.source)
        if trig_num is not None and trig_num not in chans:
            chans[trig_num] = Channel(range=trigger.channel_range,
                                      coupling=trigger.channel_coupling,
                                      probe=trigger.channel_probe,
                                      offset=trigger.channel_offset)

        for num in sorted(chans):
            ch = chans[num]
            tag = f"CHAN{num}"
            w(f":{tag}:DISP ON")
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

        self.engine_samples = mdep
        self.enabled_channels = tuple(sorted(chans))
        self._cache_preamble(self.enabled_channels)
        return mdep

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
                    f"WaveRecord did not finish within {timeout:.0f}s "
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
        """Cache each enabled channel's WORD-format y-scaling for to_volts().
        :WAV:PRE? → fmt,typ,pts,count,xinc,xorig,xref,yinc,yorig,yref."""
        assert self._inst is not None
        w, q = self.write, self.query
        w(":WAV:MODE RAW")
        w(":WAV:FORM WORD")
        self._preamble = {}
        for ch in channels:
            w(f":WAV:SOUR CHAN{ch}")
            try:
                pre = q(":WAV:PRE?").split(",")
                self._preamble[ch] = (float(pre[7]), float(pre[8]), float(pre[9]))
            except Exception as e:
                # Don't swallow silently — a timeout/garbage here almost always
                # means the scope is wedged (reboot it), and a silent identity
                # fallback would just give wrong volts and hide that.
                print(f"  warning: :WAV:PRE? for CHAN{ch} failed "
                      f"({type(e).__name__}: {e}); using identity scaling. A "
                      f"timeout here usually means the scope is wedged — reboot.",
                      file=sys.stderr)
                self._preamble[ch] = (1.0, 0.0, 0.0)         # identity fallback

    def to_volts(self, codes, channel: int):
        """Convert codes to float32 volts for `channel`, using the cached WORD
        preamble. Vectorized over any array shape. Accepts uint16 codes (16-bit
        domain) or float32 averages directly; `uint8` codes (the top byte from
        `sample_bits=8`) are shifted back to the 16-bit domain (×256) first."""
        import numpy as np
        inc, orig, ref = self._preamble.get(channel, (1.0, 0.0, 0.0))
        c = np.asarray(codes)
        scale = 256.0 if c.dtype == np.uint8 else 1.0   # 8-bit top byte → 16-bit
        return (c.astype(np.float32) * scale - ref - orig) * inc

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
