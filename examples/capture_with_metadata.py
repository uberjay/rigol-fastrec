#!/usr/bin/env python3
"""Save one externally triggered record, including settings for offline analysis."""
import argparse
from pathlib import Path

from rigol_fastrec import Channel, Trigger, WaveRecorder


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', required=True)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--channels', default='1,2', help='comma-separated analog channels')
    p.add_argument('--frames', type=int, default=4)
    p.add_argument('--samples', type=int, default=10000)
    p.add_argument('--sample-rate', type=float, default=1e9)
    p.add_argument('--range', type=float, default=8., help='full-scale volts at probe tip')
    p.add_argument('--probe', type=float, default=1., help='probe attenuation ratio')
    p.add_argument('--impedance', type=float, choices=(50., 1e6), default=1e6,
                   help='ohms for listed channels; implicit trigger stays at 1 Mohm')
    p.add_argument('--trigger-source', default='CHAN2')
    p.add_argument('--trigger-level', type=float, default=1.5)
    p.add_argument('--trigger-offset-us', type=float, default=-1.)
    p.add_argument('--timeout', type=float, default=60.)
    args = p.parse_args()
    if args.out.exists():
        p.error(f'output already exists: {args.out}')
    try:
        channels = {int(ch): Channel(range=args.range, probe=args.probe,
                                     impedance=args.impedance)
                    for ch in args.channels.split(',')}
    except ValueError as exc:
        p.error(str(exc))
    if not channels or any(ch not in (1, 2, 3, 4) for ch in channels):
        p.error('channels must be in 1..4')
    with WaveRecorder(args.host) as rec:
        rec.configure(samples=args.samples, sample_rate=args.sample_rate,
                      trigger=Trigger(source=args.trigger_source, level=args.trigger_level),
                      trigger_offset_us=args.trigger_offset_us, channels=channels)
        rec.run(args.frames, capture_metadata=True)
        print(f'Armed. Supply {args.frames} external triggers now.', flush=True)
        rec.wait_recorded(timeout=args.timeout)
        capture = rec.read_capture(count=args.frames)  # raw 16-bit, individual frames
        capture.save(args.out)
    acq = capture.metadata.acquisition
    print(f'Saved {args.out}: {acq.idn}')
    print(f'{acq.sample_rate:g} Sa/s, {acq.memory_depth} samples/frame, '
          f'channels {capture.metadata.channels}')


if __name__ == '__main__':
    main()
