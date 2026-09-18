#!/usr/bin/env python3
"""Minimal WaveRecorder capture: configure → run → wait → read → save.

Records N WaveRecord frames on one channel and writes them to a .npy, with a
short stats summary. By default it self-triggers (``--auto``: AUTO sweep) so it
runs with no DUT attached — a smoke test of the whole capture path. For a real
acquisition you wire a trigger source and fire N triggers between ``run()`` and
``wait_recorded()`` (see the marked block).

    python examples/capture_basic.py --host 10.0.80.80 --frames 64 \
        --samples 1000 --sample-rate 1e9 --out /tmp/frames.npy

Requires the rigol-fastrec package importable (``pip install -e .`` or
PYTHONPATH=python) and the scope reachable over SCPI (5555) + Frida (27042).
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from rigol_fastrec import Channel, Trigger, WaveRecorder


def progress_bar(done: int, total: int, width: int = 36) -> None:
    """One-line readback progress bar on stderr (clears itself when complete)."""
    fill = int(width * done / total)
    sys.stderr.write(f"\r  reading [{'#' * fill}{'.' * (width - fill)}] "
                     f"{done}/{total} ({done / total:4.0%})")
    if done >= total:
        sys.stderr.write("\r" + " " * (width + 28) + "\r")   # clear the line
    sys.stderr.flush()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="10.0.80.80")
    p.add_argument("--traces", type=int, default=64,
                   help="number of output traces. raw frames = traces*average.")
    p.add_argument("--samples", type=int, default=1000,
                   help="per-frame MDEP (snapped to a valid Rigol depth)")
    p.add_argument("--sample-rate", type=float, default=1e9)
    p.add_argument("--channel", type=int, default=1, help="trace channel")
    p.add_argument("--trace-range", type=float, default=0.5,
                   help="trace channel full-scale volts")
    p.add_argument("--probe", type=float, default=1.0, help="probe attenuation")
    p.add_argument("--trigger-source", default="CHAN2")
    p.add_argument("--trigger-level", type=float, default=0.5)
    p.add_argument("--trigger-slope", default="POS", choices=["POS", "NEG"])
    p.add_argument("--trigger-offset-us", type=float, default=0.0,
                   help="signed window placement: <0 pre-trigger, >0 delayed")
    p.add_argument("--crop", default=None, help="in-agent sample window LO:HI")
    p.add_argument("--average", type=int, default=1,
                   help="ship the float32 mean of each k-group")
    p.add_argument("--auto", action="store_true", default=True,
                   help="self-trigger (AUTO sweep) — runs with no DUT [default]")
    p.add_argument("--no-auto", dest="auto", action="store_false",
                   help="expect an external trigger source; fire it yourself")
    p.add_argument("--out", default=None, help="write traces to this .npy")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v: high-level ops (configure/read/MB-s); "
                        "-vv: + raw SCPI command trace")
    args = p.parse_args()

    if args.verbose:
        import logging
        from rigol_fastrec import enable_logging
        enable_logging(logging.DEBUG if args.verbose >= 2 else logging.INFO)

    crop = None
    if args.crop:
        lo, hi = (int(x) for x in args.crop.split(":"))
        crop = (lo, hi)

    trigger = Trigger(source=args.trigger_source, level=args.trigger_level,
                      slope=args.trigger_slope)

    with WaveRecorder(host=args.host) as rec:
        print(f"connected: {rec.scpi.model} fw {rec.scpi.firmware}")
        rec.configure(
            samples=args.samples, sample_rate=args.sample_rate,
            trigger=trigger, trigger_offset_us=args.trigger_offset_us,
            channels={args.channel: Channel(range=args.trace_range,
                                            probe=args.probe)},
        )
        layout = rec.channel_layout()
        print(f"layout: stride={layout.stride} enabled={list(layout.enabled)} "
              f"samples/frame={layout.samples_per_frame}")

        if args.auto:
            # No-DUT demo: AUTO sweep makes the scope self-trigger and fill all
            # frames on its own. Drop to the SCPI layer for it.
            rec.scpi.write(":TRIGger:SWEep AUTO")

        frames = args.traces * args.average
        t0 = time.monotonic()
        rec.run(frames)

        if not args.auto:
            # ---- real capture: fire your N DUT triggers here ----
            print(f"armed for {frames} frames — fire the triggers now")

        rec.wait_recorded()
        traces = rec.read(count=frames, channel=args.channel,
                          crop=crop, average=args.average, progress=progress_bar)
        dt = time.monotonic() - t0
        volts = rec.to_volts(traces, args.channel)

    print(f"read {traces.shape} {traces.dtype} in {dt:.2f}s "
          f"({frames / dt:.0f} frames/s incl. arm)")
    print(f"  samples: min={traces.min()} max={traces.max()} "
          f"mean={traces.mean():.1f}")
    print(f"  volts: min={volts.min():.4f} max={volts.max():.4f} "
          f"mean={volts.mean():.4f}")

    if args.out:
        np.save(args.out, traces)
        print(f"  saved → {args.out}")


if __name__ == "__main__":
    main()
