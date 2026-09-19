#!/usr/bin/env python3
"""Measure MHO98 WaveRecord frame timestamps using AFG1 -> CH1.

The observer reads the firmware's timestamp cache when its existing update and
SCPI getter run. It neither calls native acquisition functions nor writes memory.
Run from a checkout with the rigol_fastrec package installed (or PYTHONPATH=python).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import time

import numpy as np

from rigol_fastrec import Channel, Trigger, WaveRecorder
from .._support.bench import Validator, afg_setup, managed_afg, scpi_errors, evidence_metadata


UNITS = {'s': Decimal(1), 'ms': Decimal('1e-3'), 'us': Decimal('1e-6'),
         'µs': Decimal('1e-6'), 'μs': Decimal('1e-6'), 'ns': Decimal('1e-9'),
         'ps': Decimal('1e-12')}


def parse_timestamp(text):
    match = re.fullmatch(r'\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([a-zµμ]+)\s*', text)
    if not match or match[2] not in UNITS:
        raise ValueError(f'unsupported frame timestamp: {text!r}')
    return Decimal(match[1]) * UNITS[match[2]]


def period_summary(rows, rate):
    # Keep subtraction in integers: absolute firmware values exceed JS's exact
    # integer range on longer records. Only the final seconds are floats.
    fs = [int(row['elapsed_fs']) for row in rows]
    intervals = np.array([b-a for a, b in zip(fs, fs[1:])], dtype=np.float64)*1e-15
    error_ppm = (intervals*rate-1)*1e6
    return dict(intervals_s=intervals.tolist(),
                median_interval_s=float(np.median(intervals)),
                min_interval_s=float(np.min(intervals)),
                max_interval_s=float(np.max(intervals)),
                max_abs_period_error_ppm=float(np.max(np.abs(error_ppm))))


class FrameTimeObserver:
    def __init__(self, rec):
        if (rec.scpi.model, rec.scpi.firmware) != ('MHO98', '00.01.00'):
            raise ValueError('timestamp observer requires MHO98 firmware 00.01.00')
        self.rec = rec
        self.errors = []
        self.script = rec.readback._session.create_script(Path(__file__).with_suffix('.js').read_text())
        self.script.on('message', self._message)
        self.script.load()

    def _message(self, message, data):
        if message.get('type') == 'error':
            self.errors.append(message)

    def close(self):
        self.script.unload()

    def snapshot(self):
        if self.errors:
            raise RuntimeError(f'timestamp observer error: {self.errors}')
        return self.script.exports_sync.snapshot()

    def select(self, frame, timeout=3.):
        seq = self.snapshot()['sequence']
        start = time.monotonic()
        self.rec.scpi.write(f':RECord:WREPlay:FCURrent {frame}')
        # FCURrent? changes immediately; the timestamp cache updates via timer 3.
        # Observe that update instead of assuming an immediate SCPI query is fresh.
        while True:
            state = self.snapshot()
            update = state['update']
            if update and update['sequence'] > seq and update['frame'] == frame:
                break
            if time.monotonic()-start > timeout:
                raise TimeoutError(f'no fresh timestamp for frame {frame}')
            time.sleep(.01)
        elapsed = time.monotonic()-start
        before = int(float(self.rec.scpi.query(':RECord:WREPlay:FCURrent?')))
        text = self.rec.scpi.query(':RECord:WREPlay:FCURrent:TIME?')
        getter = self.snapshot()['getter']
        after = int(float(self.rec.scpi.query(':RECord:WREPlay:FCURrent?')))
        if before != frame or after != frame or getter != {k: v for k, v in update.items() if k != 'sequence'}:
            raise RuntimeError(f'frame changed while reading timestamp: {frame}, {before}, {after}, {getter}, {update}')
        parse_timestamp(text)
        return dict(timestamp=text, refresh_wait_s=elapsed, **getter)


def run_study(args):
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=False)
    report = dict(started=datetime.now(timezone.utc).isoformat(), cases=[])
    report.update(evidence_metadata(args))
    validator = Validator()

    def save():
        (out/'report.json').write_text(json.dumps(report, indent=2)+'\n')

    def check(name, passed, detail=''):
        report.setdefault('checks', []).append(dict(name=name, passed=bool(passed), detail=detail))
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}", flush=True)
        save()

    try:
        with WaveRecorder(args.host) as rec, managed_afg(rec, validator, [(':SOURce1', 1, 0), (':SOURce2', 3, 0)]):
            report['idn'] = rec.scpi.query('*IDN?')
            rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
            rec.scpi.write(':SOURce2:OUTPut:STATe OFF')
            rec.configure(samples=1000, sample_rate=1e9,
                          trigger=Trigger(source='CHAN1', level=0., channel_impedance=1e6),
                          channels={ch: Channel(range=4., impedance=1e6) for ch in (1, 2, 3)})
            for ch in (1, 2, 3):
                rec.scpi.write(f':CHAN{ch}:INV 0')
                rec.scpi.write(f':CHAN{ch}:UNIT VOLT')
                if float(rec.scpi.query(f':CHAN{ch}:TCAL?')) != 0:
                    rec.scpi.write(f':CHAN{ch}:TCAL 0')
            rec.scpi.write(':TRIG:SWE NORM')
            errors = scpi_errors(rec)
            if errors:
                raise RuntimeError(f'configuration rejected: {errors}')
            observer = FrameTimeObserver(rec)
            try:
                for rate, count, gap in [(2., 8, False), (137., 16, False),
                                         (13700., 32, False), (20., 24, True)]:
                    label = f'{rate:g}Hz'+('-gap' if gap else '')
                    print(f'Recording {label}, {count} frames', flush=True)
                    errors = afg_setup(rec, prefix=':SOURce1', freq=rate, vpp=1., offset=0., wave='SQU')
                    if errors:
                        raise RuntimeError(f'AFG setup rejected: {errors}')
                    rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                    actual_rate = float(rec.scpi.query(':SOURce1:FREQuency?'))
                    case = dict(label=label, rate_hz=actual_rate, frames=count, rows=[], repeats=[])
                    report['cases'].append(case)
                    rec.run(count, capture_metadata=True)
                    rec.scpi.write(':SOURce1:OUTPut:STATe ON')
                    if gap:
                        time.sleep(.22)
                        rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                        gap_start = time.monotonic()
                        time.sleep(.65)
                        rec.scpi.write(':SOURce1:OUTPut:STATe ON')
                        case['host_output_off_interval_s'] = time.monotonic()-gap_start
                    rec.wait_recorded(timeout=15.)
                    rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                    cap = rec.read_capture(count=count)
                    cap.save(out/(label+'.npz'))
                    case['sample_rate'] = cap.metadata.acquisition.sample_rate
                    rec.scpi.write(':RECord:WREPlay:OPERate STOP')
                    time.sleep(.4)
                    for frame in range(1, count+1):
                        case['rows'].append(observer.select(frame))
                    for frame in [count//2, 1, count, 2]:
                        case['repeats'].append(observer.select(frame))
                    rows = case['rows']
                    times = [int(row['elapsed_fs']) for row in rows]
                    check(label+' first frame zero', times[0] == 0)
                    check(label+' increasing times', all(a < b for a, b in zip(times, times[1:])))
                    check(label+' stable origin', len({row['first_tag'] for row in rows+case['repeats']}) == 1)
                    check(label+' reordered reads', all(int(row['elapsed_fs']) == times[row['frame']-1] for row in case['repeats']))
                    # Five significant digits: relative 1e-4 covers truncation at
                    # all mantissas. Preserve the actual residual in the report.
                    errors_s = [float(Decimal(row['elapsed_fs'])*Decimal('1e-15')-parse_timestamp(row['timestamp'])) for row in rows]
                    case['scpi_rounding_error_s'] = errors_s
                    check(label+' SCPI/native agreement', all(abs(err) <= max(t*1e-15*1e-4, 1e-12) for err, t in zip(errors_s, times)))
                    case['periods'] = period_summary(rows, actual_rate)
                    if not gap:
                        check(label+' AFG period', case['periods']['max_abs_period_error_ppm'] < 200., str(case['periods']))
                    else:
                        intervals = case['periods']['intervals_s']
                        long = [i for i, dt in enumerate(intervals) if dt > 3/actual_rate]
                        case['gap_interval_indices_zero_based'] = long
                        check(label+' inserted gap', len(long) == 1 and intervals[long[0]] > .6, str(intervals))
                        check(label+' surrounding periods', all(abs(dt*actual_rate-1) < 200e-6 for dt in intervals if dt <= 3/actual_rate))
                    after = rec.read_capture(count=count)
                    check(label+' raw record unchanged', all(np.array_equal(cap.samples[ch], after.samples[ch]) for ch in cap.samples))
                    case['errors'] = scpi_errors(rec)
                    check(label+' SCPI error queue', not case['errors'], str(case['errors']))
                    save()
            finally:
                observer.close()
        report['cleanup'] = validator.report
        check('AFG cleanup', validator.failed == 0)
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        report['cleanup'] = validator.report
        report['finished'] = datetime.now(timezone.utc).isoformat()
        save()
    return 0 if all(check['passed'] for check in report['checks']) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='10.0.10.213')
    parser.add_argument('--output-dir', required=True, help='new directory for JSON and raw NPZ captures')
    raise SystemExit(run_study(parser.parse_args()))
