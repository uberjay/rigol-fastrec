#!/usr/bin/env python3
"""Probe the stream loop's batching against sparse triggers.

The agent's stream loop (waitCaptured in agent/src/native/readback.ts) arms a
batch of N frames, then polls getPlayInfo, re-arming every 200 ms until the
hardware reports the batch ready. This probe answers: with triggers slower than
that re-arm interval, does a batch of N > 1 still complete, or does the re-arm
discard partial captures?

Method: drive a square wave from the built-in AFG into one channel and trigger
on it, NORM sweep, so the trigger rate equals the AFG frequency. For each
(rate, batch) case, stream for a fixed time and timestamp every frame. Frames
inside one batch arrive back-to-back (one C send per batch); batches are
separated by at least a trigger period. So arrival gaps recover the batch
sizes, and frames / elapsed vs the trigger rate shows whether any were lost.

Reading the numbers:

* frames ~= rate * elapsed and burst size ~= batch: partial batches survive
  the 200 ms re-arm; the capture accumulates across re-arms.
* frames ~= rate * elapsed but burst size 1: the hardware reports ready per
  frame, so a batch is delivered early. Nothing lost, batching is nominal.
* frames << rate * elapsed: re-arms are discarding partial captures.
* 0 frames (TimeoutError): the loop never sees ready at this rate.

    python tools/stream_batch_probe.py --host mho98.oodles.be
    python tools/stream_batch_probe.py --host mho98.oodles.be --rates 2,10 --batches 1,4
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from validate_scope import afg_setup, afg_off  # noqa: E402

from rigol_fastrec import WaveRecorder, Trigger, Channel  # noqa: E402


class _StreamStartFilter(logging.Filter):
    """Pass only the agent's stream telemetry ('stream start' with batch + hw
    cap, and the first few 'stream poll' transitions)."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "fastrec_stream" in record.getMessage()


def hook_agent_stream_start() -> None:
    lg = logging.getLogger("rigol_fastrec.readback")
    lg.setLevel(logging.DEBUG)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("    %(message)s"))
    h.addFilter(_StreamStartFilter())
    lg.addHandler(h)


def run_case(rec: WaveRecorder, *, channel: int, batch: int, seconds: float,
             rate: float) -> dict:
    t_arrive: list[float] = []
    dups = 0
    prev = None
    err = None
    t0 = time.monotonic()
    deadline = t0 + seconds
    try:
        for frame in rec.stream(channel=channel, batch=batch):
            now = time.monotonic()
            t_arrive.append(now)
            if prev is not None and np.array_equal(prev, frame):
                dups += 1
            prev = frame
            if now >= deadline:
                break
    except TimeoutError as e:
        err = f"TimeoutError (no frame within the 20 s socket watchdog): {e}"
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    elapsed = time.monotonic() - t0

    n = len(t_arrive)
    period = 1.0 / rate
    # Frames inside one batch arrive ~0.2 ms apart (one 2 KB record each over
    # the wire); batches are at least a trigger period or a loop cycle (~1 ms)
    # apart. The floor keeps the split meaningful above ~2 kHz.
    burst_gap = max(period / 2, 0.6e-3)
    bursts: list[int] = []
    gaps: list[float] = []
    if n:
        size = 1
        for a, b in zip(t_arrive, t_arrive[1:]):
            gap = b - a
            if gap < burst_gap:
                size += 1
            else:
                bursts.append(size)
                gaps.append(gap)
                size = 1
        bursts.append(size)
    return dict(frames=n, elapsed=elapsed, expected=rate * elapsed, dups=dups,
                bursts=bursts, gaps=gaps, err=err,
                first_at=(t_arrive[0] - t0) if n else None)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="mho98.oodles.be")
    p.add_argument("--afg-prefix", default=":SOURce1")
    p.add_argument("--afg-channel", type=int, default=1,
                   help="input channel the AFG output is cabled to (default 1)")
    p.add_argument("--afg-vpp", type=float, default=1.0)
    p.add_argument("--wave", default="SQU",
                   help="AFG function for the pacing signal (default SQU; SIN if rejected)")
    p.add_argument("--samples", type=int, default=1000)
    p.add_argument("--sample-rate", type=float, default=1e9)
    p.add_argument("--rates", default="2,10,100",
                   help="trigger rates in Hz to probe (= AFG frequency)")
    p.add_argument("--batches", default="1,4,0",
                   help="stream batch sizes to probe (0 = hardware max)")
    p.add_argument("--seconds", type=float, default=6.0, help="stream time per case")
    args = p.parse_args()

    rates = [float(r) for r in args.rates.split(",") if r.strip()]
    batches = [int(b) for b in args.batches.split(",") if b.strip()]
    ch = args.afg_channel
    hook_agent_stream_start()

    with WaveRecorder(host=args.host) as rec:
        rec.configure(samples=args.samples, sample_rate=args.sample_rate,
                      trigger=Trigger(source=f"CHAN{ch}", level=0.0, slope="POS",
                                      channel_range=4.0),
                      channels={ch: Channel(range=4.0)})
        rec.scpi.write(":TRIGger:SWEep NORM")
        lay = rec.channel_layout()
        print(f"host {args.host}: CHAN{ch} trigger, NORM sweep, "
              f"samples/frame={lay.samples_per_frame}, stride={lay.stride}, "
              f"FMAX={rec.max_frames()}")

        try:
            for rate in rates:
                errs = afg_setup(rec, prefix=args.afg_prefix, freq=rate,
                                 vpp=args.afg_vpp, offset=0.0, wave=args.wave)
                wave = args.wave
                if errs and args.wave != "SIN":
                    print(f"  AFG rejected {errs}; falling back to SIN")
                    wave = "SIN"
                    errs = afg_setup(rec, prefix=args.afg_prefix, freq=rate,
                                     vpp=args.afg_vpp, offset=0.0, wave=wave)
                if errs:
                    print(f"  AFG errors: {errs}")
                time.sleep(0.5)
                print(f"\ntrigger rate {rate:g} Hz ({wave}, period {1e3 / rate:.0f} ms):")
                for batch in batches:
                    print(f"  batch={batch}:")
                    r = run_case(rec, channel=ch, batch=batch,
                                 seconds=args.seconds, rate=rate)
                    if r["err"]:
                        print(f"    {r['err']}")
                    b = r["bursts"]
                    g = r["gaps"]
                    print(f"    frames={r['frames']} in {r['elapsed']:.1f} s "
                          f"(expected ~{r['expected']:.0f} at {rate:g} Hz); "
                          f"first frame after {r['first_at']:.2f} s"
                          if r["first_at"] is not None else
                          f"    frames=0 in {r['elapsed']:.1f} s")
                    if b:
                        print(f"    bursts={len(b)} size min/med/max="
                              f"{min(b)}/{int(np.median(b))}/{max(b)}; "
                              f"inter-burst gap med={np.median(g) * 1e3:.0f} ms"
                              if g else
                              f"    bursts=1 size={b[0]} (single burst)")
                    print(f"    consecutive identical frames: {r['dups']}")
        finally:
            afg_off(rec, prefix=args.afg_prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
