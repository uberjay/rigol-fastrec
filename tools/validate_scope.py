#!/usr/bin/env python3
"""On-scope validation for rigol_fastrec -- exercises the full capture/readback
path against real hardware and asserts it behaves correctly. See
docs/VALIDATION.md for the bench setup and pass criteria.

Four groups of checks:

* **Self-consistency** (no signal needed): shapes/dtypes, crop == full slice,
  averaging == numpy mean of the raw frames (with a frame count chosen to span
  a readback chunk boundary), multichannel single-shot == per-channel reads,
  and the error paths (disabled channel, crop past end).

* **Lane mapping** (built-in AFG): drive a known sine into one input channel and
  assert that channel's readback reconstructs the right frequency and carries
  the signal, while the other enabled channels stay quiet. That proves the
  deinterleave lane mapping -- the one thing the self-consistency checks can't,
  since a wrong-but-consistent offset would fool them. With a dual-output AFG,
  drive a SECOND channel at a distinct frequency (--afg-channel2) so each
  channel must report its own frequency -- a direct cross-talk / swap test.

* **Streaming** (agent-driven continuous capture): pull a few frames off
  WaveRecorder.stream() per encoding (raw / packed / 8-bit / crop / multichannel),
  confirm frames re-capture (vary) rather than replay a stale buffer, check the
  streamed frequency with the AFG, and confirm a normal read() still works after
  the stream stops (export restored). Skip with --no-stream.

* **Metadata and physical scaling**: save/load raw captures and preambles,
  verify actual settings and crop/encoding, measure known AFG amplitude/DC with
  both impedances and probe settings, and exercise invalidated/interrupted
  records. Skip with --no-metadata, or run alone with --metadata-only.

--output-dir saves a JSON report and NPZ evidence. Both owned AFG outputs are
switched off on exit, including failures and Ctrl-C; scope settings are not
restored to their pre-test values.

    # full default run (assumes AFG1→CHAN1, AFG2→CHAN3, nothing on CHAN2):
    # exercises every check -- lane mapping, cross-talk, the combinations, streaming
    python tools/validate_scope.py --host 10.0.10.213

    # nothing connected: self-consistency + streaming checks only
    python tools/validate_scope.py --host 10.0.10.213 --no-afg

    # single-output AFG cabled to CHAN1
    python tools/validate_scope.py --host 10.0.10.213 --afg-channel2 0

    # the subtlest lane case: non-contiguous enable set (gap at lane 2)
    python tools/validate_scope.py --host 10.0.10.213 \
        --channels 1,2,4 --afg-channel 1 --afg-channel2 4

Requires the package importable (`pip install -e .`) and the scope
reachable over SCPI (5555) + frida-server (27042).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import numpy as np

from rigol_fastrec import Channel, Trigger, WaveRecorder
from rigol_fastrec.exceptions import AgentError


class Validator:
    """Tiny PASS/FAIL harness: each check prints a line; exit code reflects
    whether any failed. A raised exception inside a check is itself a FAIL, so
    one broken step doesn't abort the rest of the run."""

    def __init__(self, output_dir=None, configuration=None) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.output_dir = output_dir
        self.report = dict(started_utc=datetime.now(timezone.utc).isoformat(),
                           configuration=configuration or {}, checks=[])
        self.save_report()

    def save_report(self):
        if self.output_dir is not None:
            self.report.update(passed=self.passed, failed=self.failed, skipped=self.skipped)
            (self.output_dir / 'report.json').write_text(json.dumps(self.report, indent=2)+'\n')

    def skip(self, name, detail):
        self.skipped += 1
        self.report['checks'].append(dict(name=name, status='SKIP', detail=detail))
        print(f'  [SKIP] {name} -- {detail}', flush=True)
        self.save_report()

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        tag = "PASS" if ok else "FAIL"
        self.passed += ok
        self.failed += not ok
        print(f"  [{tag}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
        self.report['checks'].append(dict(name=name, status=tag, detail=detail))
        self.save_report()
        return ok

    def run(self, name: str, fn) -> bool:
        """Run a check function that returns (ok, detail); trap exceptions."""
        try:
            ok, detail = fn()
        except Exception as e:  # a throwing check is a failure, not a crash
            return self.check(name, False, f"raised {type(e).__name__}: {e}")
        return self.check(name, ok, detail)

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} checks passed"
              + (f", {self.failed} FAILED" if self.failed else " -- all good")
              + (f", {self.skipped} skipped" if self.skipped else ""))
        self.report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        self.save_report()
        return 1 if self.failed else 0


@contextmanager
def validation_session(v):
    try:
        yield
    except (Exception, KeyboardInterrupt) as exc:
        v.check('validation session completed', False, f'{type(exc).__name__}: {exc}')
    finally:
        v.save_report()


@contextmanager
def managed_afg(rec, v, sources):
    """Turn off every AFG this run owns, even when capture raises or is interrupted."""
    try:
        yield
    finally:
        for prefix, _, _ in sources:
            def shutdown(prefix=prefix):
                rec.scpi.write(f'{prefix}:OUTPut:STATe OFF')
                state = rec.scpi.query(f'{prefix}:OUTPut:STATe?')
                return state in ('0', 'OFF'), f'{prefix} output={state}'
            v.run(f'{prefix} output disabled on exit', shutdown)


# --- built-in AFG control (SCPI) ---------------------------------------------
# The exact mnemonics vary by model/option. These are the standard Rigol AFG
# forms; confirm against your scope on the first `-vv` run (they log at DEBUG),
# and override --afg-prefix / the output command if needed.

def afg_drain_errors(rec: WaveRecorder, limit: int = 20) -> None:
    """Empty the SCPI error queue so stale errors don't confuse afg_setup."""
    for _ in range(limit):
        if rec.scpi.query(":SYSTem:ERRor?").lstrip().startswith("0"):
            break


def afg_setup(rec: WaveRecorder, *, prefix: str, freq: float, vpp: float,
              offset: float, wave: str = "SIN", impedance: float = 1e6) -> list[tuple[str, str]]:
    """Program the built-in AFG. Returns [(command, scope-error), …] for any
    command the scope rejected -- so a wrong mnemonic is reported precisely
    instead of silently producing no signal. Mnemonics vary by model; if one
    errors, check the programming guide and adjust here / pass --afg-prefix."""
    # Headers per the DHO800/900 (== MHO900) programming guide, :SOURce subsystem.
    cmds = [
        f"{prefix}:OUTPut:STATe OFF",
        f"{prefix}:IMPedance {'FIFTy' if impedance == 50 else 'OMEG'}",
        f"{prefix}:MOD:STATe OFF",
        f"{prefix}:FUNCtion {wave}",
        f"{prefix}:FREQuency {freq:g}",
        f"{prefix}:VOLTage:AMPLitude {vpp:g}",  # peak-to-peak
        f"{prefix}:VOLTage:OFFSet {offset:g}",
        f"{prefix}:OUTPut:STATe ON",            # output enable
    ]
    afg_drain_errors(rec)
    errs = []
    for c in cmds:
        rec.scpi.write(c)
        e = rec.scpi.query(":SYSTem:ERRor?")
        if not e.lstrip().startswith("0"):      # 0,"No error" → fine
            errs.append((c, e))
    return errs


def afg_off(rec: WaveRecorder, *, prefix: str) -> None:
    try:
        rec.scpi.write(f"{prefix}:OUTPut:STATe OFF")
    except Exception:
        pass


# --- signal helpers ----------------------------------------------------------

def fundamental_hz(row: np.ndarray, fs: float) -> float:
    """Dominant frequency of one trace row (Hz), via a windowed rFFT. Phase- and
    amplitude-independent, so it works on AUTO-sweep frames at random phase."""
    x = row.astype(np.float64)
    x -= x.mean()
    if len(x) < 4 or not np.any(x):
        return 0.0
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    k = int(np.argmax(spec[1:])) + 1          # skip DC
    return k * fs / len(x)


def median_p2p_volts(rec: WaveRecorder, codes: np.ndarray, ch: int) -> float:
    """Median per-row peak-to-peak amplitude, in volts."""
    v = rec.to_volts(codes, ch)
    return float(np.median(v.max(axis=1) - v.min(axis=1)))


def measured_hz(codes: np.ndarray, fs: float, rows: int = 16) -> float:
    """Dominant frequency of a channel's frames (median of per-row rFFTs)."""
    n = min(len(codes), rows)
    return float(np.median([fundamental_hz(codes[i], fs) for i in range(n)]))


def take_stream(rec: WaveRecorder, n: int, **kw) -> list:
    """Pull the first n frames off rec.stream(**kw), then stop the stream
    cleanly. gen.close() raises GeneratorExit at the suspended yield, running
    the generator's finally (streamStop + data-socket teardown)."""
    frames: list = []
    gen = rec.stream(**kw)
    try:
        for f in gen:
            frames.append(f)
            if len(frames) >= n:
                break
    finally:
        gen.close()
    return frames


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="The default options assume AFG1 (SOURce1) is connected to CHAN1 "
               "and AFG2 (SOURce2) is connected to CHAN3, with nothing on CHAN2 "
               "(the undriven cross-talk witness). This default exercises every "
               "check: 4-channel-mode lane mapping, dual-source cross-talk, the "
               "multichannel + crop + average + encoding combinations, and "
               "streaming. Run with --no-afg for the self-consistency + streaming "
               "checks with nothing connected, or --afg-channel2 0 for a "
               "single-output AFG.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="10.0.10.213")
    p.add_argument("--channels", default="1,2,3",
                   help="comma-separated trace channels to enable (default 1,2,3, "
                        "which gives 4-channel FPGA mode); AFG channels must be in it")
    p.add_argument("--trigger-source", default="CHAN2")
    p.add_argument("--samples", type=int, default=1000)
    p.add_argument("--sample-rate", type=float, default=1e9)
    p.add_argument("--frames", type=int, default=600,
                   help="frames to record (kept modest; spans >1 readback chunk)")
    p.add_argument("--average", type=int, default=7,
                   help="k for the averaging check; pick one that does NOT divide "
                        "the chunk size so a k-group straddles a chunk boundary")
    p.add_argument("--range", type=float, default=8.0,
                   help="per-channel full-scale volts (8 div); wide enough for the AFG")
    # AFG / lane mapping
    p.add_argument("--afg-channel", type=int, default=1,
                   help="input channel AFG1 (SOURce1) is cabled to (default 1); "
                        "drives the AFG lane-mapping check")
    p.add_argument("--no-afg", dest="afg", action="store_false",
                   help="skip AFG setup + the lane-mapping check (self-consistency only)")
    p.add_argument("--afg-prefix", default=":SOURce1")
    p.add_argument("--afg-vpp", type=float, default=2.0)
    p.add_argument("--afg-offset", type=float, default=0.0)
    p.add_argument("--afg-periods", type=float, default=12.0,
                   help="how many signal periods to fit in one frame (sets AFG freq)")
    p.add_argument("--afg-channel2", type=int, default=3,
                   help="input channel AFG2 (SOURce2) is cabled to (default 3); drives "
                        "a DISTINCT frequency to prove channels don't cross-contaminate. "
                        "Pass 0 for a single-output AFG")
    p.add_argument("--afg-prefix2", default=":SOURce2")
    p.add_argument("--afg-periods2", type=float, default=7.0,
                   help="periods/frame for the 2nd source (distinct from --afg-periods)")
    # streaming (agent-driven continuous capture)
    p.add_argument("--no-stream", dest="stream", action="store_false",
                   help="skip the streaming checks (agent-driven continuous capture)")
    p.add_argument("--stream-frames", type=int, default=8,
                   help="frames to pull per streaming check (default 8)")
    metadata = p.add_mutually_exclusive_group()
    metadata.add_argument('--no-metadata', action='store_true', help='skip metadata/measurement checks')
    metadata.add_argument('--metadata-only', action='store_true', help='only the new capture checks')
    p.add_argument('--csv', action='store_true',
                   help="also export Rigol Record CSVs via Frida+ADB and compare every sample")
    p.add_argument('--adb', default='adb', help='ADB executable for --csv')
    p.add_argument('--adb-serial', help='ADB serial (default HOST:55555)')
    p.add_argument('--csv-timeout', type=float, default=90., help='seconds per CSV export')
    p.add_argument('--output-dir', type=Path,
                   help='new directory for JSON results and metadata capture NPZ files')
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v: ops; -vv: + raw SCPI/agent firehose")
    args = p.parse_args()

    if args.csv and args.no_metadata:
        p.error('--csv requires metadata checks')
    if not 1 <= args.csv_timeout <= 295:
        p.error('--csv-timeout must be 1..295 seconds')

    if args.verbose:
        import logging
        from rigol_fastrec import enable_logging
        enable_logging(logging.DEBUG if args.verbose >= 2 else logging.INFO)

    # --afg-channel2 0 (or negative) → single-output AFG (no 2nd source).
    if args.afg_channel2 is not None and args.afg_channel2 < 1:
        args.afg_channel2 = None

    enabled = [int(c) for c in args.channels.split(",") if c.strip()]
    if not enabled or len(set(enabled)) != len(enabled) or any(c not in (1, 2, 3, 4) for c in enabled):
        p.error('--channels must contain distinct channel numbers in 1..4')
    if args.frames < 1 or not 1 <= args.average <= args.frames:
        p.error('require --frames >= --average >= 1')
    if args.samples < 4 or not 0 < args.sample_rate <= 4e9:
        p.error('require at least 4 samples and 0 < sample rate <= 4e9')
    use_afg = args.afg and args.afg_channel is not None
    if args.afg and args.afg_channel is None:
        print("note: no --afg-channel given → skipping the lane-mapping check "
              "(pass --afg-channel N, or --no-afg to silence this)\n")

    window_s = args.samples / args.sample_rate
    # driven = {input channel: expected frequency}; one or two AFG sources, each
    # at a distinct frequency so a swap/cross-talk shows up as the wrong freq.
    driven: dict[int, float] = {}
    afg_sources: list[tuple[str, int, float]] = []   # (scpi prefix, channel, freq)
    if use_afg:
        driven[args.afg_channel] = args.afg_periods / window_s
        afg_sources.append((args.afg_prefix, args.afg_channel,
                            driven[args.afg_channel]))
        if args.afg_channel2 is not None:
            driven[args.afg_channel2] = args.afg_periods2 / window_s
            afg_sources.append((args.afg_prefix2, args.afg_channel2,
                                driven[args.afg_channel2]))
    if args.afg_channel2 == args.afg_channel and args.afg_channel2 is not None:
        print("error: --afg-channel2 must differ from --afg-channel", file=sys.stderr)
        return 2
    off_set = [c for c in driven if c not in enabled]
    if off_set:
        print(f"error: AFG channel(s) {off_set} not in --channels {enabled}",
              file=sys.stderr)
        return 2

    if args.output_dir is not None:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            p.error(f'cannot create new output directory: {exc}')
    v = Validator(args.output_dir, {k: str(val) if isinstance(val, Path) else val
                                   for k, val in vars(args).items()})
    print(f"validating {args.host}: channels={enabled} trigger={args.trigger_source} "
          f"{args.samples} samples @ {args.sample_rate:g} Sa/s, {args.frames} frames")
    for pfx, c, f in afg_sources:
        print(f"AFG {pfx}: {f:g} Hz sine, {args.afg_vpp} Vpp → CHAN{c}")

    with validation_session(v), WaveRecorder(host=args.host) as rec, managed_afg(rec, v, afg_sources):
        print(f"connected: {rec.scpi.model} fw {rec.scpi.firmware}\n")
        v.report['instrument'] = rec.scpi.idn
        if not args.metadata_only:
            legacy_checks(rec, args, v, enabled, driven, afg_sources, use_afg)
        if not args.no_metadata:
            from validate_captures import run_metadata_checks
            run_metadata_checks(rec, args, v, enabled, afg_sources, afg_setup)


    return v.summary()


def legacy_checks(rec, args, v, enabled, driven, afg_sources, use_afg):
    chan_cfg = {c: Channel(range=args.range) for c in enabled}
    rec.configure(samples=args.samples, sample_rate=args.sample_rate,
                  trigger=Trigger(source=args.trigger_source, level=1.5),
                  channels=chan_cfg)
    mdep = rec.channel_layout().samples_per_frame
    live = rec.channel_layout()
    print(f"layout: stride={live.stride} enabled={list(live.enabled)} "
          f"samples/frame={mdep}\n")

    afg_errs = []
    if use_afg:
        for pfx, _c, f in afg_sources:
            afg_errs += afg_setup(rec, prefix=pfx, freq=f,
                                  vpp=args.afg_vpp, offset=args.afg_offset)
        if afg_errs:
            print("  ⚠ the scope REJECTED these AFG commands -- fix the "
                  "mnemonics in afg_setup() (or --afg-prefix):")
            for c, e in afg_errs:
                print(f"      {c}  →  {e}")
            print("  (the lane-mapping checks below will fail until the AFG "
                  "actually outputs)\n")
        v.check('AFG setup accepted', not afg_errs, str(afg_errs))

    # Self-trigger (AUTO sweep) so no external trigger is needed; the AFG
    # signal is captured at random phase, which the FFT freq check tolerates.
    rec.scpi.write(":TRIGger:SWEep AUTO")
    rec.run(args.frames)
    rec.wait_recorded(timeout=max(10.0, args.frames / 50.0))
    actual_rate = float(rec.scpi.query(':ACQ:SRAT?'))

    ch = args.afg_channel if use_afg else enabled[0]
    F, k = args.frames, args.average

    # --- Tier 0/1: self-consistency -------------------------------------
    print("self-consistency:")
    raw = rec.read(count=F, channel=ch)

    v.check("raw shape/dtype",
            raw.shape == (F, mdep) and raw.dtype == np.uint16,
            f"{raw.shape} {raw.dtype}, expected ({F}, {mdep}) uint16")

    def determinism():
        raw2 = rec.read(count=F, channel=ch)
        ok = np.array_equal(raw, raw2)
        return ok, "identical" if ok else "two reads of the same record DIFFER"
    v.run("re-read determinism", determinism)

    def crop_check():
        lo, hi = mdep // 4, mdep // 2
        cropped = rec.read(count=F, channel=ch, crop=(lo, hi))
        ok = np.array_equal(cropped, raw[:, lo:hi])
        return ok, f"crop({lo},{hi}) == full[:, {lo}:{hi}]"
    v.run("crop == full slice", crop_check)

    def average_check():
        navg = (F // k) * k
        expect = raw[:navg].reshape(F // k, k, mdep).mean(axis=1)
        avg = rec.read(count=F, channel=ch, average=k)
        stats = rec.readback.last_read_stats
        v.report['averaging_readback'] = stats
        chunk = int(stats['chunk'])
        if F > chunk and chunk % k and k > 1:
            v.check('averaging crosses a hardware readback chunk', True,
                    f'{F} frames, chunk={chunk}, average={k}, hardware cap={stats["hwMaxFrameCount"]}')
        else:
            v.skip('averaging across a chunk boundary',
                   f'frames={F}, chunk={chunk}, average={k}; increase frames / choose non-divisor')
        ok = (avg.dtype == np.float32 and avg.shape == (F // k, mdep)
              and np.allclose(avg, expect, rtol=0, atol=0.5))
        md = float(np.max(np.abs(avg - expect))) if avg.shape == expect.shape else -1
        return ok, f"k={k}, max|Δ|={md:.3g} codes (expect ~0)"
    v.run("average == numpy mean of raw", average_check)

    def packed_check():
        # 16-bit packed == full 16-bit with the low 4 bits dropped (top 12).
        p = rec.read(count=F, channel=ch, sample_bits=16, transport="packed")
        ok = p.dtype == np.uint16 and np.array_equal(p, raw & 0xFFF0)
        nbad = int(np.count_nonzero(p != (raw & 0xFFF0))) \
            if p.shape == raw.shape else -1
        return ok, f"packed == 16-bit top-12 bits ({nbad} mismatched samples)"
    v.run("16-bit packed == top-12 bits", packed_check)

    def u8_check():
        # 8-bit read == the full 16-bit read's high byte (code>>8).
        u8 = rec.read(count=F, channel=ch, sample_bits=8)
        expect = (raw >> 8).astype(np.uint8)
        ok = u8.dtype == np.uint8 and np.array_equal(u8, expect)
        nbad = int(np.count_nonzero(u8 != expect)) \
            if u8.shape == raw.shape else -1
        return ok, f"8-bit == 16-bit high byte ({nbad} mismatched samples)"
    v.run("8-bit == 16-bit high byte (code>>8)", u8_check)

    # --- combinations: the C does crop/average/encoding together, but these
    #     combos have never been exercised on hardware on their own ---------
    lo, hi = mdep // 4, mdep // 2

    def crop_average_check():
        navg = (F // k) * k
        expect = raw[:navg, lo:hi].reshape(F // k, k, hi - lo).mean(axis=1)
        got = rec.read(count=F, channel=ch, crop=(lo, hi), average=k)
        ok = got.shape == expect.shape and np.allclose(got, expect, rtol=0, atol=0.5)
        md = float(np.max(np.abs(got - expect))) if got.shape == expect.shape else -1
        return ok, f"crop({lo},{hi})+avg(k={k}) == numpy (max|Δ|={md:.3g})"
    v.run("crop + average == numpy mean", crop_average_check)

    def crop_packed_check():
        got = rec.read(count=F, channel=ch, crop=(lo, hi), transport="packed")
        ok = got.dtype == np.uint16 and np.array_equal(got, raw[:, lo:hi] & 0xFFF0)
        return ok, f"crop({lo},{hi}) + packed == cropped top-12 bits"
    v.run("crop + packed == cropped top-12", crop_packed_check)

    def crop_8bit_check():
        got = rec.read(count=F, channel=ch, crop=(lo, hi), sample_bits=8)
        expect = (raw[:, lo:hi] >> 8).astype(np.uint8)
        ok = got.dtype == np.uint8 and np.array_equal(got, expect)
        return ok, f"crop({lo},{hi}) + 8-bit == cropped high byte"
    v.run("crop + 8-bit == cropped high byte", crop_8bit_check)

    if len(enabled) > 1:
        def demux_check():
            multi = rec.read(count=F, channels=enabled)
            bad = [c for c in enabled
                   if not np.array_equal(multi[c], rec.read(count=F, channel=c))]
            return not bad, ("mismatched channels: " + str(bad) if bad
                             else f"single-shot {enabled} == per-channel reads")
        v.run("multichannel demux == per-channel", demux_check)

        def multi_average_check():
            # per-channel cross-chunk accumulators in one multichannel pass
            multi = rec.read(count=F, channels=enabled, average=k)
            bad = [c for c in enabled if not np.allclose(
                multi[c], rec.read(count=F, channel=c, average=k), rtol=0, atol=0.5)]
            return not bad, ("mismatch: " + str(bad) if bad else
                             f"single-shot {enabled} avg(k={k}) == per-channel")
        v.run("multichannel + average == per-channel", multi_average_check)

        def multi_crop_check():
            multi = rec.read(count=F, channels=enabled, crop=(lo, hi))
            bad = [c for c in enabled if not np.array_equal(
                multi[c], rec.read(count=F, channel=c, crop=(lo, hi)))]
            return not bad, ("mismatch: " + str(bad) if bad else
                             f"single-shot {enabled} crop == per-channel")
        v.run("multichannel + crop == per-channel", multi_crop_check)

    def crop_past_end():
        try:
            rec.read(count=F, channel=ch, crop=(0, mdep + 8))
        except ValueError:
            return True, "crop past samples_per_frame → ValueError"
        return False, "expected ValueError, got none"
    v.run("crop past end rejected", crop_past_end)

    # Use the LIVE enabled set (configure() also enables the trigger channel
    # and disables the rest), not the requested list, to pick a truly-off one.
    live_enabled = list(rec.channel_layout().enabled)
    disabled = next((c for c in (1, 2, 3, 4) if c not in live_enabled), None)
    if disabled is not None:
        def disabled_channel():
            try:
                rec.read(count=F, channel=disabled)
            except AgentError:
                return True, f"reading disabled CHAN{disabled} → AgentError"
            return False, "expected AgentError, got none"
        v.run("disabled channel rejected", disabled_channel)

    # --- Tier 2: AFG lane-mapping check ---------------------------------
    if use_afg:
        print("\nlane mapping (AFG):")
        # One single-shot multichannel read; every check below inspects ITS
        # demuxed per-channel arrays, so this validates the multichannel path.
        all_ch = rec.read(count=F, channels=enabled) if len(enabled) > 1 \
            else {ch: rec.read(count=F, channel=ch)}
        p2p = {c: median_p2p_volts(rec, all_ch[c], c) for c in enabled}
        freq = {c: measured_hz(all_ch[c], actual_rate) for c in enabled}
        print("    per-channel  "
              + "  ".join(f"CH{c}: {p2p[c]:.3f} V / {freq[c]/1e6:.2f} MHz"
                          for c in enabled))

        # Each DRIVEN channel must carry ITS OWN frequency. With two sources
        # at distinct freqs, a swap or interleave bug makes a channel report
        # the other source's frequency → it fails its own check here.
        for c, fexp in driven.items():
            v.check(f"CHAN{c} carries its own {fexp/1e6:.3g} MHz signal",
                    p2p[c] > 0.2 and abs(freq[c] - fexp) <= 0.05 * fexp,
                    f"p2p={p2p[c]:.3f} V, freq={freq[c]:.4g} vs {fexp:.4g} Hz")
        # Non-driven enabled channels must stay quiet (no cross-talk / no
        # mis-routed signal landing where nothing is connected).
        quiet_lim = max(0.2, 0.2 * min(p2p[c] for c in driven))
        for c in enabled:
            if c not in driven:
                v.check(f"CHAN{c} quiet (no cross-talk)",
                        p2p[c] < quiet_lim,
                        f"p2p={p2p[c]:.3f} V (< {quiet_lim:.3f})")

    # --- Tier 3: streaming (agent-driven continuous capture) ------------
    # Bounded checks: pull a few frames off each stream, then stop. The
    # stream drives capture itself (SetRun mode=2 + getPlayInfo) and brackets
    # it in export mode, so the last check confirms a normal read() still
    # works afterward (export restored, engine sane). AUTO sweep (set above)
    # keeps triggers flowing so frames arrive promptly.
    if args.stream:
        print("\nstreaming:")
        sN = max(2, args.stream_frames)

        def stream_shape():
            fr = take_stream(rec, sN, channel=ch)
            ok = (len(fr) == sN and all(
                f.shape == (mdep,) and f.dtype == np.uint16 for f in fr))
            return ok, f"{len(fr)} frames, each ({mdep},) uint16"
        v.run("stream raw shape/dtype", stream_shape)

        def stream_live():
            fr = take_stream(rec, max(4, sN), channel=ch)
            varies = len(fr) >= 2 and any(
                not np.array_equal(fr[0], f) for f in fr[1:])
            return varies, ("consecutive frames differ (live re-capture)" if varies
                            else "frames identical -- replaying a stale buffer?")
        v.run("stream re-captures (frames vary)", stream_live)

        def stream_packed():
            fr = take_stream(rec, max(2, sN // 2), channel=ch, transport="packed")
            ok = all(f.shape == (mdep,) and f.dtype == np.uint16
                     and int(np.bitwise_and(f, 0xF).max()) == 0 for f in fr)
            return ok, f"{len(fr)} packed frames → uint16, low 4 bits zero"
        v.run("stream + packed", stream_packed)

        def stream_8bit():
            fr = take_stream(rec, max(2, sN // 2), channel=ch, sample_bits=8)
            ok = all(f.shape == (mdep,) and f.dtype == np.uint8 for f in fr)
            return ok, f"{len(fr)} frames, ({mdep},) uint8"
        v.run("stream + 8-bit", stream_8bit)

        def stream_crop():
            fr = take_stream(rec, max(2, sN // 2), channel=ch, crop=(lo, hi))
            ok = all(f.shape == (hi - lo,) and f.dtype == np.uint16 for f in fr)
            return ok, f"{len(fr)} frames cropped to {hi - lo} samples"
        v.run("stream + crop", stream_crop)

        if len(enabled) > 1:
            def stream_multi():
                fr = take_stream(rec, max(2, sN // 2), channels=enabled)
                ok = all(isinstance(f, dict) and set(f) == set(enabled)
                         and all(f[c].shape == (mdep,) for c in enabled)
                         for f in fr)
                return ok, f"{len(fr)} frames, dict{{{enabled}}} each ({mdep},)"
            v.run("stream multichannel demux", stream_multi)

        if use_afg and ch in driven:
            def stream_freq():
                fr = take_stream(rec, max(8, sN), channel=ch)
                fhz = measured_hz(np.asarray(fr), float(rec.scpi.query(':ACQ:SRAT?')))
                fexp = driven[ch]
                return abs(fhz - fexp) <= 0.05 * fexp, \
                    f"streamed freq {fhz:.4g} vs {fexp:.4g} Hz"
            v.run(f"stream CHAN{ch} reconstructs {driven[ch]/1e6:.3g} MHz",
                  stream_freq)

        def read_after_stream():
            r = rec.read(count=min(8, F), channel=ch)
            ok = r.dtype == np.uint16 and r.shape[1] == mdep
            return ok, "one-shot read() works after streaming (export restored)"
        v.run("read() works after stream", read_after_stream)


if __name__ == "__main__":
    sys.exit(main())
