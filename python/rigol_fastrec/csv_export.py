"""Independent Rigol WaveRecord CSV export, retrieval, and strict comparison.

Requires exclusive scope use and an accessible ADB server on the instrument.
This invokes Rigol's CSV writer; it does NOT serialize fastrec's sample arrays.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid

import numpy as np


@dataclass(frozen=True)
class RecordCsv:
    samples: dict[int, np.ndarray]  # volts, [frame, sample]
    t0: float                     # common origin from the CSV header
    increment: float


@dataclass(frozen=True)
class CsvExport:
    path: Path
    scope_path: str
    sha256: str
    waveform: RecordCsv
    writer_status: dict


def parse_record_csv(path, *, frames: int, samples: int) -> RecordCsv:
    """Parse the observed MHO98 Record CSV format; never guess a screen layout."""
    if frames < 1 or samples < 1:
        raise ValueError('positive frame/sample counts required')
    with Path(path).open(newline='', encoding='utf-8-sig') as f:
        rows = csv.reader(f)
        header = next(rows, [])
        channels = []
        while len(channels) < len(header):
            match = re.fullmatch(r'CH([1-4])V', header[len(channels)].strip())
            if match is None:
                break
            channels.append(int(match[1]))
        if not channels or len(set(channels)) != len(channels):
            raise ValueError('expected distinct CHnV Record CSV columns')
        rest = header[len(channels):]
        if len(rest) != 3 or rest[2].strip():
            raise ValueError('expected Record CSV t0/tInc header')
        fields = []
        for cell, name in zip(rest, ('t0', 'tInc')):
            match = re.fullmatch(r'\s*'+name+r'\s*=\s*(\S+)\s*', cell)
            if match is None:
                raise ValueError(f'missing {name} in Record CSV header')
            fields.append(float(match[1]))
        t0, increment = fields
        if not math.isfinite(t0) or not math.isfinite(increment) or increment <= 0:
            raise ValueError('invalid Record CSV time header')
        count = frames*samples
        data = np.empty((count, len(channels)), dtype=np.float64)
        n = 0
        for row in rows:
            if n >= count:
                raise ValueError(f'CSV has more than {count} rows; wrong record/depth')
            if (len(row) != len(channels)+2 or any(c.strip() for c in row[len(channels):])):
                raise ValueError(f'malformed Record CSV row {n+2}')
            data[n] = [float(v) for v in row[:len(channels)]]
            n += 1
        if n != count:
            raise ValueError(f'CSV has {n} rows, expected {count}; truncated/wrong record')
        if not np.isfinite(data).all():
            raise ValueError('non-finite CSV samples')
    shaped = data.reshape(frames, samples, len(channels))
    return RecordCsv({ch: shaped[:, :, i].copy() for i, ch in enumerate(channels)}, t0, increment)


def compare_record_csv(capture, reference: RecordCsv, *, tolerance_lsb: float = .5) -> dict:
    """Compare every sample, without fitting gains, shifting, or dropping tails.

    LSB means one uint16 WORD code at the saved y_increment. The default
    tolerance is half a code step, accommodating decimal preamble/CSV rounding.
    Values outside the tolerance fail the comparison.
    """
    if not math.isfinite(tolerance_lsb) or tolerance_lsb <= 0:
        raise ValueError('positive finite tolerance_lsb required')
    m = capture.metadata
    if (m.average != 1 or m.sample_bits != 16 or m.transport != 'raw'
            or m.crop != (0, m.acquisition.memory_depth) or m.read_frames != m.recorded_frames):
        raise ValueError('CSV comparison requires the complete raw 16-bit record')
    if set(reference.samples) != set(capture.samples):
        raise ValueError('CSV channel set differs from capture')
    result = {'channels': {}, 't0_s': reference.t0, 'increment_s': reference.increment}
    for ch, data in capture.samples.items():
        ref = reference.samples[ch]
        if data.shape != ref.shape:
            raise ValueError(f'CH{ch} CSV frame/sample shape differs')
        if not np.isfinite(ref).all():
            raise ValueError(f'CH{ch} non-finite CSV samples')
        pre = m.acquisition.channel(ch).preamble
        if not np.isclose(reference.increment, 1/m.acquisition.sample_rate, rtol=1e-6, atol=0):
            raise ValueError('CSV sample interval differs from actual acquisition rate')
        # Compare the common CSV origin with the saved preamble.
        origin = pre.x_origin-pre.x_reference*pre.x_increment
        if not np.isclose(reference.t0, origin, rtol=1e-6, atol=reference.increment*1e-5):
            raise ValueError(f'CH{ch} CSV t0 differs from saved preamble')
        delta = np.abs(capture.to_volts(ch).astype(np.float64)-ref)
        limit = abs(pre.y_increment)*tolerance_lsb
        bad = int(np.count_nonzero(delta > limit))
        result['channels'][ch] = dict(values=int(delta.size), max_error_v=float(delta.max()),
                                     max_error_lsb=float(delta.max()/abs(pre.y_increment)),
                                     tolerance_lsb=tolerance_lsb, mismatched_values=bad)
        if bad:
            raise ValueError(f'CH{ch}: {bad}/{delta.size} CSV samples exceed {tolerance_lsb:g} '
                             f'WORD LSB (max {delta.max()/abs(pre.y_increment):.6g})')
    return result


class _Adb:
    def __init__(self, executable, serial):
        self.executable, self.serial = executable, serial

    def call(self, *args, connect=False, timeout=15):
        cmd = [self.executable] + ([] if connect else ['-s', self.serial]) + list(args)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        return result.stdout.strip()

    def files(self, paths):
        # All remote paths are internally generated ASCII names, never user text.
        cmd = 'for p in ' + ' '.join(paths) + '; do if [ -f "$p" ]; then stat -c "%n %s" "$p"; fi; done'
        result = {}
        for line in self.call('shell', cmd).splitlines():
            name, size = line.rsplit(' ', 1)
            if name not in paths or int(size) < 0:
                raise RuntimeError(f'unexpected ADB file listing: {line}')
            result[name] = int(size)
        return result


def _clean_errors(rec):
    errors = []
    for _ in range(20):
        value = rec.scpi.query(':SYSTem:ERRor?')
        if value.lstrip().startswith('0'):
            if errors:
                raise RuntimeError(f'SCPI errors during CSV export: {errors}')
            return
        errors.append(value)
    raise RuntimeError(f'SCPI error queue would not drain: {errors}')


def export_record_csv(rec, path, *, adb='adb', adb_serial=None, timeout=90.,
                      max_values=1_000_000) -> CsvExport:
    """Export the complete, metadata-bound record to a NEW local CSV file.

    Slow diagnostic path: bounded by max_values (frames × samples × channels),
    with unique scope filenames and no automatic overwriting. Remote file is
    removed only after a complete local copy, parse and settings check succeed.
    ADB defaults to host:55555. Scope UI storage settings are left changed.
    Do not touch the UI, reconfigure, stream or read concurrently with export.
    """
    record = rec._record_metadata
    if record is None:
        raise ValueError('run(capture_metadata=True) and wait_recorded() before CSV export')
    if not math.isfinite(timeout) or not 1 <= timeout <= 295:
        raise ValueError('CSV timeout must be 1..295 seconds')
    values = rec._record_frames*record.memory_depth*len(record.channels)
    if max_values < 1 or values > max_values:
        raise ValueError(f'CSV export has {values} values, exceeds budget {max_values}')
    executable = shutil.which(adb)
    if executable is None:
        raise FileNotFoundError(f'ADB executable not found: {adb}')
    path = Path(path)
    # Reserve destination before any instrument mutation; never overwrite.
    with path.open('xb') as output:
        copied = False
        try:
            _clean_errors(rec)
            rec._check_settings(record, rec.scpi.snapshot(), check_scaling=True)
            if rec.scpi.recorded_frames() != rec._record_frames:
                raise ValueError('recorded frame count changed before CSV export')
            if rec.scpi.query(':SAVE:STAT?') != '1':
                raise RuntimeError('scope is already saving; wait before CSV export')
            serial = adb_serial or f'{rec.scpi._host}:55555'
            bridge = _Adb(executable, serial)
            if ':' in serial:
                bridge.call('connect', serial, connect=True)
            if bridge.call('get-state') != 'device':
                raise RuntimeError(f'ADB device not ready: {serial}')
            stem = 'fr'+uuid.uuid4().hex[:16]
            # Overlap OFF auto-numbers; Overlap ON uses the exact filename.
            # Use a fresh name and verify BOTH absent without changing that setting.
            candidates = [f'/data/UserData/{stem}{suffix}.csv' for suffix in ('', '0')]
            if bridge.files(candidates):
                raise FileExistsError('generated CSV filename already exists on scope')
            deadline = time.monotonic()+timeout
            rec.readback.arm_record_csv(timeout+3)
            try:
                rec.scpi.write(f':SAVE:MEMory:WAVeform C:/{stem}.csv')
                previous = None
                while time.monotonic() < deadline:
                    writer = rec.readback.record_csv_status()
                    status = rec.scpi.query(':SAVE:STAT?')
                    files = bridge.files(candidates)
                    if len(files) > 1:
                        raise RuntimeError('ambiguous CSV export filenames')
                    if any(size > values*32+4096 for size in files.values()):
                        raise RuntimeError('CSV export exceeds expected size budget')
                    if writer['expired']:
                        raise TimeoutError('Record CSV selection/export timed out')
                    complete = (writer['redirected'] == writer['started'] == writer['finished'] == 1)
                    if complete and status == '1' and files and min(files.values()) > 0 and files == previous:
                        break
                    previous = files
                    time.sleep(.2)
                else:
                    raise TimeoutError('CSV save did not finish; scope export may still be running')
                _clean_errors(rec)
                scope_path, size = next(iter(files.items()))
                with tempfile.TemporaryDirectory(prefix='fastrec-csv-') as temp:
                    pulled = Path(temp)/'record.csv'
                    bridge.call('pull', scope_path, str(pulled), timeout=max(1., deadline-time.monotonic()))
                    if pulled.stat().st_size != size or bridge.files(candidates) != files:
                        raise RuntimeError('CSV changed/truncated during transfer')
                    with pulled.open('rb') as source:
                        shutil.copyfileobj(source, output)
                    output.flush()
                    copied = True
                waveform = parse_record_csv(path, frames=rec._record_frames, samples=record.memory_depth)
                if set(waveform.samples) != {c.channel for c in record.channels}:
                    raise ValueError('CSV contains a different channel set')
                rec._check_settings(record, rec.scpi.snapshot(), check_scaling=True)
                if rec.scpi.recorded_frames() != rec._record_frames:
                    raise ValueError('recorded frame count changed during CSV export')
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                bridge.call('shell', 'rm -- '+scope_path)  # only our verified unique file
                return CsvExport(path, scope_path, digest, waveform, writer)
            finally:
                rec.readback.disarm_record_csv()
        except BaseException:
            rec._invalidate_capture()
            if not copied:
                path.unlink(missing_ok=True)
            raise
