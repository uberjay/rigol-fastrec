#!/usr/bin/env python3
"""Compare bulk replay-header timestamps with the per-frame reference.

Studies: pacing, native Frida tracing, and a short-prefix timestamp pass.
AFG1 must be connected to CH1. Both AFG outputs are disabled on exit.
Reports retain every header, DMA result, mismatch, and optional native event.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import time

import numpy as np
from rigol_fastrec import WaveRecorder, Channel, Trigger
from .frame_timestamps import FrameTimeObserver
from .._support.timestamps import register_reference
from .._support.bench import afg_setup, managed_afg, Validator, scpi_errors, evidence_metadata

TAG_MASK = (1 << 48) - 1


def decode_block(data, *, first, count, samples, layout, last_tag):
    """Decode observed header format, retaining explicit 48-bit tag values.

    The candidate association uses header i+1 for frame i, and the final
    register tag for the last frame. Comparisons must verify this association.
    """
    stride = layout['stride']
    if count < 1 or samples < 1:
        raise ValueError('count and samples must be positive')
    frame_bytes = samples * stride * 2 + 16
    if len(data) != count * frame_bytes:
        raise ValueError('incomplete header DMA response')
    headers = [struct.unpack_from('<8H', data, i * frame_bytes) for i in range(count)]
    if any(h[0] != 0xfa05 or h[4] != (first+i) % 65536 for i, h in enumerate(headers)):
        raise ValueError('header marker or frame index mismatch')
    raw = [(h[5] << 32) | (h[6] << 16) | h[7] for h in headers]
    ticks = raw[1:] + [int(last_tag) & TAG_MASK]
    channels = layout['enabledList']
    values = {ch: np.empty((count, samples), dtype='<u2') for ch in channels}
    for i in range(count):
        frame = np.frombuffer(data, dtype='<u2', count=samples*stride,
                              offset=i*frame_bytes+16).reshape(samples, stride)
        for ch in channels:
            lane = ch-1 if stride >= 4 else channels.index(ch)
            values[ch][i] = frame[:, lane]
    return headers, ticks, values


def study_variants(study):
    if study == 'pacing':
        return [(f'interval-{n}fs', False, {}, n) for n in
                (10000, 10**9, 10**10, 10**11, 10**12)]
    if study == 'trace':
        return [
            ('plain', False, {}, 10000),
            ('traced', True, {}, 10000),
            ('no-poll', False, {'pollStages': False}, 10000),
            ('traced-no-poll', True, {'pollStages': False}, 10000),
            ('settled', False, {'pollStages': False, 'settleMs': 20}, 10000),
            ('delay-dma', False, {'pollStages': False, 'armDelayMs': 2}, 10000),
            ('split-dma', False, {'pollStages': False, 'chunkFrames': 1}, 10000),
            ('split-dma-delayed', False, {'pollStages': False, 'chunkFrames': 1,
                                         'chunkDelayMs': 1}, 10000),
            ('paced', False, {'pollStages': False}, 10**10),
        ]
    return [('prefix-paced', False, {'pollStages': False, 'txSamples': 32}, 10**10),
            ('prefix-unpaced', False, {'pollStages': False, 'txSamples': 32}, 10000)]


class HeaderProbe:
    def __init__(self, rec):
        self.blocks = []
        self.traces = []
        self.errors = []
        self.script = rec.readback._session.create_script(Path(__file__).with_suffix('.js').read_text())
        self.script.on('message', self._message)
        self.script.load()

    def _message(self, message, data):
        if data is not None:
            self.blocks.append(bytes(data))
        elif message.get('type') == 'error':
            self.errors.append(message)
        elif message.get('payload', {}).get('type') == 'trace':
            self.traces.append(message['payload'])

    def read(self, first, count, samples, layout, interval, options, trace):
        attempts = []
        for _ in range(4):
            self.blocks.clear()
            self.traces.clear()
            self.errors.clear()
            meta = self.script.exports_sync.read(first, count, samples*layout['stride']*2,
                                                 0, 16, str(interval), options)
            attempts.append(meta)
            if meta['ret'] > 0:
                break
            time.sleep(.001)
        deadline = time.monotonic()+2
        while meta['ret'] > 0 and (not self.blocks or (trace and not self.traces)):
            if self.errors or time.monotonic() >= deadline:
                break
            time.sleep(.001)
        if self.errors or len(self.blocks) != 1 or (trace and len(self.traces) != 1):
            raise RuntimeError(f'header read failed: {attempts}; {self.errors}')
        headers, ticks, values = decode_block(self.blocks[0], first=first, count=count,
                    samples=samples, layout=layout, last_tag=meta['afterDma']['ticks'])
        detail = dict(first=first, count=count, headers=headers, attempts=attempts,
                      traces=self.traces.copy())
        return ticks, values, detail

    def close(self):
        self.script.unload()


def main(args):
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=False)
    report = dict(started=datetime.now(timezone.utc).isoformat(), study=args.study, cases=[])
    report.update(evidence_metadata(args))
    cleanup = Validator()

    def save():
        (out/'report.json').write_text(json.dumps(report, indent=2)+'\n')

    cases = [([1, 2, 3], 10000, 32, False)]
    if args.study == 'prefix':
        cases = [([1, 2, 3], 1000, 600, False), ([1], 10000, 64, False),
                 ([1, 3], 10000, 64, False), ([1, 2, 3], 10000, 64, False),
                 ([1, 2, 3], 100000, 64, False), ([1, 2, 3], 1000000, 8, False),
                 ([1, 2, 3], 1000, 24, True)]
    try:
        with WaveRecorder(args.host) as rec, managed_afg(rec, cleanup,
                [(':SOURce1', 1, 13700), (':SOURce2', 3, 0)]):
            report['idn'] = rec.scpi.query('*IDN?')
            for channels, samples, count, gap in cases:
                label = '-'.join(map(str, channels))+f'-{samples}'+('-gap' if gap else '')
                print('CASE', label, count, flush=True)
                rec.scpi.write(':SOURce2:OUTPut:STATe OFF')
                rec.configure(samples=samples, sample_rate=1e9,
                    trigger=Trigger(source='CHAN1', level=.2, channel_impedance=1e6),
                    channels={ch: Channel(range=4., impedance=1e6) for ch in channels})
                rec.scpi.write(':TRIG:SWE NORM')
                errors = afg_setup(rec, prefix=':SOURce1', freq=20. if gap else 13700.,
                                   vpp=1., offset=0., wave='SQU')
                if errors:
                    raise RuntimeError(errors)
                if gap:
                    rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                rec.run(count, capture_metadata=True)
                if gap:
                    rec.scpi.write(':SOURce1:OUTPut:STATe ON')
                    time.sleep(.22)
                    rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                    time.sleep(.65)
                    rec.scpi.write(':SOURce1:OUTPut:STATe ON')
                rec.wait_recorded(20)
                rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                probe = HeaderProbe(rec)
                try:
                    probe.script.exports_sync.watch(True)
                    baseline = rec.read_capture(count=count)
                    captured = probe.script.exports_sync.watch(False)
                    hwmax = rec.readback.last_read_stats['hwMaxFrameCount']
                    layout = rec.readback.channel_layout()
                    baseline.save(out/(label+'.npz'))
                    case = dict(label=label, samples=samples, channels=channels, frames=count,
                                hwmax=hwmax, layout=layout, captured_args=captured, reads=[])
                    report['cases'].append(case)
                    if args.study == 'prefix':
                        values, refs = register_reference(rec, count, samples, channels)
                        if not all(np.array_equal(values[ch], baseline.samples[ch]) for ch in channels):
                            raise AssertionError('register reference changed waveform samples')
                        case['reference'] = 'per-frame register'
                    else:
                        rec.scpi.write(':RECord:WREPlay:OPERate STOP')
                        time.sleep(.4)
                        observer = FrameTimeObserver(rec)
                        try:
                            rows = [observer.select(frame) for frame in range(1, count+1)]
                        finally:
                            observer.close()
                        case['reference'] = 'SCPI/display native field'
                        case['reference_observations'] = rows
                        refs = [int(row['first_tag'])+int(row['elapsed_fs'])//250000 for row in rows]
                    case['reference_ticks'] = refs
                    if gap:
                        intervals = [(b-a)*250e-12 for a, b in zip(refs, refs[1:])]
                        case['intervals_s'] = intervals
                        if sum(dt > .6 for dt in intervals) != 1:
                            raise AssertionError(f'expected one deliberate trigger gap: {intervals}')
                    save()
                    for name, trace, options, interval in study_variants(args.study):
                        probe.script.exports_sync.trace(trace)
                        for rep in range(args.repeats):
                            first = rep % 2 if args.study == 'prefix' else 0
                            length = count-first if args.study == 'prefix' else min(25, count, hwmax)
                            ranges = [(i, min(hwmax, first+length-i))
                                      for i in range(first, first+length, hwmax)]
                            if args.study == 'prefix' and rep % 3 == 2:
                                ranges.reverse()
                            actual_samples = options.get('txSamples', samples)
                            row = dict(name=name, rep=rep, interval_fs=interval, errors=[],
                                       samples_match=True, transfers=[])
                            case['reads'].append(row)
                            start = time.monotonic()
                            for base, n in ranges:
                                ticks, values, detail = probe.read(base, n, actual_samples, layout,
                                                                   interval, options, trace)
                                row['transfers'].append(detail)
                                row['errors'].extend([base+i, tick, refs[base+i] & TAG_MASK]
                                    for i, tick in enumerate(ticks) if tick != (refs[base+i] & TAG_MASK))
                                row['samples_match'] &= all(np.array_equal(values[ch],
                                    baseline.samples[ch][base:base+n, :actual_samples]) for ch in channels)
                                save()
                            row['host_s'] = time.monotonic()-start
                            print(name, rep, 'mismatches', len(row['errors']),
                                  'samples', row['samples_match'], flush=True)
                    probe.script.exports_sync.trace(False)
                    after = rec.read_capture(count=count)
                    case['original_unchanged'] = all(np.array_equal(baseline.samples[ch], after.samples[ch])
                                                     for ch in channels)
                    case['scpi_errors'] = scpi_errors(rec)
                    save()
                finally:
                    probe.close()
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        report['cleanup'] = cleanup.report
        report['finished'] = datetime.now(timezone.utc).isoformat()
        save()
    bad = cleanup.failed or any(not c['original_unchanged'] or c['scpi_errors'] or
        any(r['errors'] or not r['samples_match'] for r in c['reads']) for c in report['cases'])
    return 1 if bad else 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='10.0.10.213')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--study', choices=['pacing', 'trace', 'prefix'], default='prefix',
                        help='prefix (default): short replay; pacing/trace: full-depth diagnostics')
    parser.add_argument('--repeats', type=int, default=6)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('--repeats must be positive')
    return args


if __name__ == '__main__':
    raise SystemExit(main(parse_args()))
