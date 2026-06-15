#!/usr/bin/env python3
"""Readback throughput benchmark over the WaveRecorder facade.

Self-triggers (AUTO sweep) to fill frames with no DUT by default (`--no-auto`
expects an external trigger source), then times read() across
a sweep of batch sizes — raw and, optionally, agent-averaged — reporting frames/s
and MB/s over the wire. Useful for sanity-checking the readback path and
comparing against the ~11.7 MB/s ceiling of the built-in 100Mb ethernet.

`--frames` is the number of OUTPUT (averaged) traces wanted; each row records
`batch × average` raw frames and reads back `batch` traces. So with `--average`,
a batch of B records B×k raw frames (mind the scope's max-recordable-frames at
your MDEP for large B×k).

    python examples/throughput_bench.py --host 10.0.80.80 \
        --samples 1000 --traces 16,64,256 --average 1,8

`frames/s` counts raw frames recorded+read; each row's MB/s is the bytes that
actually crossed the socket (post-crop, post-average) over the read() wall time.
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


def _csv_ints(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="10.0.80.80")
    p.add_argument("--samples", type=int, default=1000)
    p.add_argument("--sample-rate", type=float, default=1e9)
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--traces", type=_csv_ints, default=[16, 64, 256],
                   help="comma-separated OUTPUT trace counts to time; each records "
                        "traces×average raw frames")
    p.add_argument("--average", type=_csv_ints, default=[1],
                   help="comma-separated k values (1 = raw)")
    p.add_argument("--sample-bits", type=int, default=16, choices=[16, 8],
                   help="raw sample resolution: 16 (uint16) or 8 (top byte, -50%%)")
    p.add_argument("--transport", default="raw", choices=["raw", "packed"],
                   help="raw, or packed (16-bit only: top 12 bits, -25%%); "
                        "both only affect average=1 rows")
    p.add_argument("--crop", default=None, help="sample window LO:HI")
    p.add_argument("--auto", action="store_true", default=True,
                   help="self-trigger (AUTO sweep) — runs with no DUT [default]")
    p.add_argument("--no-auto", dest="auto", action="store_false",
                   help="expect an external trigger source (NORM sweep)")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v: high-level ops (configure/read/MB-s); "
                        "-vv: + raw SCPI command trace")
    args = p.parse_args()

    if args.sample_bits == 8 and args.transport == "packed":
        raise SystemExit("--sample-bits 8 is incompatible with --transport packed "
                         "(8-bit samples are already byte-aligned)")

    if args.verbose:
        import logging
        from rigol_fastrec import enable_logging
        enable_logging(logging.DEBUG if args.verbose >= 2 else logging.INFO)

    crop = None
    if args.crop:
        lo, hi = (int(x) for x in args.crop.split(":"))
        crop = (lo, hi)

    trigger = Trigger(source="CHAN2", level=0.5)

    with WaveRecorder(host=args.host) as rec:
        print(f"connected: {rec.scpi.model} fw {rec.scpi.firmware}")
        rec.configure(samples=args.samples, sample_rate=args.sample_rate,
                      trigger=trigger,
                      channels={args.channel: Channel(range=0.5)})
        if args.auto:
            rec.scpi.write(":TRIGger:SWEep AUTO")   # self-fill, no DUT
        # else: NORM (set by begin_wave_record) — frames fill from an external trigger
        layout = rec.channel_layout()
        per_frame = layout.samples_per_frame if not crop else (crop[1] - crop[0])
        print(f"layout: stride={layout.stride} samples/frame={per_frame}\n")

        print(f"{'out':>8} {'k':>4} {'raw':>9} {'read_s':>8} "
              f"{'frames/s':>10} {'MB/s':>8}")
        fmax = rec.max_frames()          # cached at configure(); cap per record
        for n in args.traces:           # n = OUTPUT (averaged) traces wanted
            for k in args.average:
                raw = n * k              # raw frames to record: n traces × k each
                if raw > fmax:           # one record can't hold this many frames
                    print(f"{n:>8} {k:>4} {raw:>9}  (skip: > {fmax} max/record)")
                    continue
                t0 = time.monotonic()
                rec.run(raw)
                rec.wait_recorded(timeout=max(10.0, raw / 50.0))
                t_read0 = time.monotonic()
                traces = rec.read(count=raw, channel=args.channel, crop=crop,
                                  average=k, sample_bits=args.sample_bits,
                                  transport=args.transport, progress=progress_bar)
                read_s = time.monotonic() - t_read0
                # Actual bytes over the socket (not traces.nbytes — packed/8-bit
                # don't round-trip to the host array size): per record, u32 prefix
                # + payload. packed = 2 samples → 3 B, 8-bit = 1 B, 16-bit = 2 B,
                # float32 avg = 4 B.
                nrec, slen = traces.shape[0], traces.shape[1]
                if k > 1:
                    payload = slen * 4
                elif args.sample_bits == 8:
                    payload = slen
                elif args.transport == "packed":
                    payload = ((slen + 1) // 2) * 3
                else:
                    payload = slen * 2
                wire_bytes = nrec * (4 + payload)
                mbps = wire_bytes / max(1e-6, read_s) / 1e6
                fps = raw / max(1e-6, time.monotonic() - t0)  # raw frames/s
                print(f"{n:>8} {k:>4} {raw:>9} {read_s:>8.3f} "
                      f"{fps:>10.0f} {mbps:>8.1f}")


if __name__ == "__main__":
    main()
