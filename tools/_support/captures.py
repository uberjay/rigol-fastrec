"""Metadata/CSV checks used by tools.validate_scope."""
from __future__ import annotations

from pathlib import Path
import json
import tempfile

import numpy as np

from rigol_fastrec import Capture, Channel, MetadataError, ScalingError, ScopeRunTimeout, Trigger

from .bench import scpi_errors


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def expect_error(kind, fn, match=''):
    try:
        fn()
    except kind as exc:
        matches = (match,) if isinstance(match, str) else match
        require(any(text in str(exc) for text in matches), f'unexpected error: {exc}')
        return True, f'{kind.__name__}: {exc}'
    raise AssertionError(f'expected {kind.__name__}, operation succeeded')


def sine_fit(volts, fs, frequency):
    """Fit known-frequency sine + DC; residual catches an incorrect time scale/lane."""
    t = np.arange(volts.shape[-1]) / fs
    phase = 2*np.pi*frequency*t
    design = np.column_stack((np.sin(phase), np.cos(phase), np.ones_like(t)))
    rows = np.atleast_2d(volts)[:4].astype(np.float64)
    coeff = np.linalg.lstsq(design, rows.T, rcond=None)[0]
    p2p = 2*np.hypot(coeff[0], coeff[1])
    residual = np.sqrt(np.mean((design @ coeff-rows.T)**2, axis=0))
    return float(np.median(p2p)), float(np.median(coeff[2])), float(np.median(residual))


class CaptureChecks:
    def __init__(self, rec, args, v, enabled, sources, setup, archive_dir):
        self.rec, self.args, self.v = rec, args, v
        self.enabled, self.sources, self.setup = enabled, sources, setup
        self.archive_dir = Path(archive_dir)
        self.frames = 8
        self.cap = None
        self.freqs = {ch: 1e6 if i == 0 else .7e6 for i, (_, ch, _) in enumerate(sources)}

    def clean(self):
        errors = scpi_errors(self.rec)
        require(not errors, f'SCPI rejected commands: {errors}')

    def configure(self, *, impedance=1e6, probe=1., channels=None, trigger=None,
                  samples=10000, rate=100e6):
        if getattr(self.args, 'csv', False):
            require(self.rec.scpi.query(':SAVE:STAT?') == '1', 'scope is still exporting CSV')
        self.rec.scpi.stop_record()
        self.clean()
        selected = self.enabled if channels is None else channels
        source = self.args.trigger_source if trigger is None else trigger
        self.rec.configure(samples=samples, sample_rate=rate,
                           trigger_offset_us=-50.,
                           trigger=Trigger(source=source, level=.2*probe,
                                           channel_impedance=impedance),
                           channels={ch: Channel(range=8*probe, probe=probe,
                                                 offset=.3*probe, impedance=impedance)
                                     for ch in selected})
        # The harness owns its test settings; do not inherit inversion/deskew/units.
        for ch in self.rec.scpi.enabled_channels:
            for suffix in ('INV 0', 'UNIT VOLT'):
                self.rec.scpi.write(f':CHAN{ch}:{suffix}')
            # 00.01.00 can reject TCAL writes at slower timebases even for zero.
            # Do not write a setting already at zero; always verify the result.
            if float(self.rec.scpi.query(f':CHAN{ch}:TCAL?')) != 0:
                self.rec.scpi.write(f':CHAN{ch}:TCAL 0')
            require(float(self.rec.scpi.query(f':CHAN{ch}:TCAL?')) == 0,
                    f'CH{ch} deskew could not be reset')
        self.rec.scpi.write(':TRIG:SWE AUTO')
        self.clean()
        for prefix, ch, _ in self.sources:
            errors = self.setup(self.rec, prefix=prefix, freq=self.freqs[ch],
                                vpp=1., offset=.2, impedance=impedance)
            require(not errors, f'AFG setup rejected: {errors}')
        self.clean()

    def record(self):
        self.rec.run(self.frames, capture_metadata=True)
        self.rec.wait_recorded(timeout=15.)
        cap = self.rec.read_capture(count=self.frames)
        self.clean()
        return cap

    def archive(self, cap, label):
        path = self.archive_dir / (label+'.npz')
        cap.save(path)
        loaded = Capture.load(path)
        require(loaded.metadata == cap.metadata, 'metadata changed in NPZ round trip')
        for ch in cap.samples:
            require(np.array_equal(loaded.samples[ch], cap.samples[ch]), f'CH{ch} raw codes changed')
            require(np.array_equal(loaded.to_volts(ch), cap.to_volts(ch)), f'CH{ch} volts changed')
        if self.v.output_dir is not None:
            self.v.report.setdefault('captures', []).append(path.name)
        return loaded

    def csv(self, cap, label):
        if not getattr(self.args, 'csv', False):
            return
        from rigol_fastrec.csv_export import compare_record_csv
        print(f'  exporting Rigol Record CSV: {label} ...', flush=True)
        export = self.rec.export_csv(self.archive_dir/(label+'.csv'), adb=self.args.adb,
                                     adb_serial=self.args.adb_serial, timeout=self.args.csv_timeout)
        comparison = compare_record_csv(cap, export.waveform)
        after = self.rec.read_capture(count=cap.metadata.recorded_frames)
        require(all(np.array_equal(cap.samples[ch], after.samples[ch]) for ch in cap.samples),
                'record changed during CSV export')
        evidence = dict(file=export.path.name, scope_path=export.scope_path,
                        sha256=export.sha256, writer_status=export.writer_status,
                        raw_record_unchanged=True, comparison=comparison)
        (self.archive_dir/(label+'-csv.json')).write_text(json.dumps(evidence, indent=2)+'\n')
        self.v.report.setdefault('csv_exports', []).append(evidence)
        values = sum(c['values'] for c in comparison['channels'].values())
        worst = max(c['max_error_lsb'] for c in comparison['channels'].values())
        self.v.check(label+' same-record Rigol CSV', True,
                     f'{values} values; max error {worst:.4g} WORD LSB; record unchanged')

    def initial(self):
        self.configure()
        self.cap = self.record()
        self.archive(self.cap, 'metadata-baseline')
        self.csv(self.cap, 'metadata-baseline')
        m = self.cap.metadata
        return True, (f'{m.recorded_frames} frames, {m.channels}, '
                      f'{m.acquisition.memory_depth} samples @ {m.acquisition.sample_rate:g} Sa/s')

    def provenance(self):
        m = self.cap.metadata
        a = m.acquisition
        require(a.idn == self.rec.scpi.query('*IDN?'), 'instrument identity mismatch')
        require(a.sample_rate == float(self.rec.scpi.query(':ACQ:SRAT?')), 'actual rate mismatch')
        require(a.memory_depth == int(float(self.rec.scpi.query(':ACQ:MDEP?'))), 'depth mismatch')
        require(m.requested_sample_rate == 100e6 and m.requested_samples == 10000, 'request lost')
        require(m.recorded_frames == self.rec.scpi.recorded_frames() == self.frames, 'frame count mismatch')
        require(len(m.agent_sha256) == 64 and m.package_version and m.host_ready_utc <= m.host_read_utc,
                'incomplete host provenance')
        for ch in a.channels:
            require(ch.preamble.points == a.memory_depth, 'preamble depth disagrees')
            require(np.isclose(ch.preamble.x_increment*a.sample_rate, 1, rtol=1e-5), 'interval mismatch')
            require(ch.settings.impedance == 1e6, 'impedance mismatch')
        return True, f'actual/requested rate={a.sample_rate:g}/{m.requested_sample_rate:g}; full preambles'

    def preamble_axis(self):
        for ch in self.cap.samples:
            pre = self.cap.metadata.acquisition.channel(ch).preamble
            lo, hi = self.cap.metadata.crop
            axis = self.cap.time_axis(ch, reference='scpi_preamble')
            require(axis.shape == (hi-lo,), 'wrong preamble axis length')
            require(np.isclose(axis[0], (lo-pre.x_reference)*pre.x_increment+pre.x_origin,
                               rtol=1e-12, atol=pre.x_increment*1e-6), 'preamble origin differs')
            require(np.allclose(np.diff(axis), pre.x_increment, rtol=1e-9, atol=0),
                    'preamble sample spacing differs')
        return True, 'all saved channels: origin, reference and sample spacing'

    def arrays(self):
        cap = self.cap
        require(set(cap.samples) == set(self.rec.channel_layout().enabled), 'default omitted channel')
        require(int(cap.metadata.acquisition.trigger_source[4:]) in cap.samples, 'trigger missing')
        for ch, data in cap.samples.items():
            require(np.array_equal(data, self.rec.read(count=self.frames, channel=ch)), 'legacy codes differ')
            require(np.array_equal(cap.to_volts(ch), self.rec.to_volts(data, ch)), 'live/saved scaling differs')
        return True, 'all enabled channels including trigger; identical to legacy read()'

    def encoding(self, label, **kw):
        lo, hi = 123, 789
        cap = self.rec.read_capture(count=self.frames, crop=(lo, hi), **kw)
        for ch, data in cap.samples.items():
            expected = self.cap.samples[ch][:, lo:hi]
            if kw.get('average', 1) > 1:
                k = kw['average']
                expected = expected.reshape(self.frames//k, k, hi-lo).mean(axis=1)
                require(np.allclose(data, expected, rtol=0, atol=.5), 'average differs')
            else:
                if kw.get('sample_bits') == 8:
                    expected = (expected >> 8).astype(np.uint8)
                elif kw.get('transport') == 'packed':
                    expected = expected & 0xfff0
                require(np.array_equal(data, expected), 'encoded codes differ')
            axis = cap.time_axis(ch)
            require(np.allclose(axis, np.arange(lo, hi)/cap.metadata.acquisition.sample_rate,
                                rtol=1e-12, atol=0), 'crop time axis lost original indices')
        self.archive(cap, label)
        return True, f'{kw or "raw"}, crop=({lo},{hi}), all saved channels'

    def physical(self, cap, ch, probe=1., expected_vpp=1., expected_dc=.2):
        a = cap.metadata.acquisition
        amp, dc, residual = sine_fit(cap.to_volts(ch), a.sample_rate, self.freqs[ch])
        require(abs(amp-expected_vpp*probe) <= .10*expected_vpp*probe+.02*probe,
                f'CH{ch} {amp:g} Vpp; expected {expected_vpp*probe:g}')
        require(abs(dc-expected_dc*probe) <= .04*probe,
                f'CH{ch} DC={dc:g}; expected {expected_dc*probe:g}')
        require(residual < .04*probe, f'CH{ch} sine residual={residual:g} V (wrong frequency/time scale?)')
        return True, f'CH{ch}: {amp:.5g} Vpp, DC={dc:.5g} V, residual={residual:.3g} V'

    def impedance_and_probe(self, impedance, probe):
        self.configure(impedance=impedance, probe=probe)
        cap = self.record()
        for ch in cap.metadata.acquisition.channels:
            require(ch.settings.impedance == impedance, 'wrong impedance in metadata')
            require(ch.settings.probe == probe, 'wrong probe ratio in metadata')
        self.archive(cap, f'impedance-{impedance:g}-probe-{probe:g}')
        self.csv(cap, f'impedance-{impedance:g}-probe-{probe:g}')
        details = [self.physical(cap, ch, probe)[1] for ch in self.freqs]
        return True, '; '.join(details)

    def implicit_trigger(self):
        ch = next(iter(self.freqs))
        others = [n for n in self.enabled if n != ch]
        self.configure(impedance=50, channels=others, trigger=f'CHAN{ch}')
        cap = self.record()
        require(ch in cap.samples and cap.metadata.acquisition.channel(ch).settings.impedance == 50,
                'implicit trigger not saved or not terminated')
        self.archive(cap, 'implicit-trigger-50ohm')
        return self.physical(cap, ch)

    def noncontiguous(self):
        # With the default wiring, {1,3,4} exercises a physical gap without recabling.
        chosen = set(self.freqs)
        chosen.add(next(ch for ch in (4, 3, 2, 1) if ch not in chosen))
        chosen = sorted(chosen)
        self.configure(channels=chosen, trigger=f'CHAN{chosen[0]}')
        cap = self.record()
        layout = self.rec.channel_layout()
        require(layout.stride == 4 and set(cap.samples) == set(chosen), 'not a gapped 4-lane capture')
        require(layout.offsets == {ch: ch-1 for ch in chosen}, 'public offsets disagree')
        for ch in self.freqs:
            self.physical(cap, ch)
        for ch in set(chosen)-set(self.freqs):
            require(np.ptp(cap.to_volts(ch), axis=1).max() < .2, 'signal leaked into undriven lane')
        self.archive(cap, 'noncontiguous')
        self.csv(cap, 'noncontiguous')
        return True, f'{chosen}, physical offsets={layout.offsets}; both AFG frequencies distinct'

    def normal_trigger(self):
        ch = next(iter(self.freqs))
        self.configure(trigger=f'CHAN{ch}')
        self.rec.scpi.write(':TRIG:SWE NORM')
        cap = self.record()
        require(cap.metadata.acquisition.trigger_sweep == 'NORM', 'did not use real edge triggers')
        self.archive(cap, 'normal-edge-trigger')
        self.csv(cap, 'normal-edge-trigger')
        return self.physical(cap, ch)

    def inverted(self):
        ch = next(iter(self.freqs))
        self.configure()
        self.rec.scpi.write(f':CHAN{ch}:INV 1')
        self.rec.scpi.write(f':CHAN{ch}:TCAL 1e-8')
        cap = self.record()
        channel = cap.metadata.acquisition.channel(ch)
        require(channel.inverted and np.isclose(channel.deskew_s, 1e-8, rtol=1e-5, atol=0),
                'inversion/deskew metadata not preserved')
        self.archive(cap, 'inverted-deskew')
        self.csv(cap, 'inverted-deskew')
        return self.physical(cap, ch, expected_dc=-.2)

    def nonvoltage(self):
        ch = self.enabled[0]
        self.configure()
        self.rec.scpi.write(f':CHAN{ch}:UNIT AMP')
        cap = self.record()
        require(cap.metadata.acquisition.channel(ch).units == 'AMP', 'unit change rejected')
        return expect_error(ScalingError, lambda: cap.to_volts(ch), 'not volts')

    def stream_invalidates(self):
        self.configure()
        self.record()
        stream = self.rec.stream(channel=self.enabled[0], batch=2)
        try:
            next(stream)
        finally:
            stream.close()
        return expect_error(MetadataError, lambda: self.rec.read_capture(count=self.frames))

    def stale(self):
        self.configure()
        return expect_error(MetadataError, lambda: self.rec.read_capture(count=self.frames))

    def settings_drift(self, phase):
        self.configure()
        self.rec.run(self.frames, capture_metadata=True)
        if phase == 'before_read':
            self.rec.wait_recorded(timeout=15)
        ch = self.enabled[0]
        self.rec.scpi.write(f':CHAN{ch}:OFFS 0.5')
        require(np.isclose(float(self.rec.scpi.query(f':CHAN{ch}:OFFS?')), .5), 'offset edit rejected')
        fn = (lambda: self.rec.read_capture(count=self.frames)) if phase == 'before_read' else self.rec.wait_recorded
        # Firmware may discard an in-progress record when vertical settings
        # change, so frame-count rejection can correctly precede settings checks.
        reasons = ('settings changed',) if phase == 'before_read' else (
            'settings changed', 'completed frame count')
        result = expect_error(MetadataError, fn, reasons)
        expect_error(MetadataError, lambda: self.rec.read_capture(count=self.frames))
        self.clean()
        return result

    def interrupted(self, *, stop):
        self.configure()
        # NORM sweep + unreachable level: no trigger even with the 1 Vpp AFG connected.
        self.rec.scpi.write(':TRIG:EDGE:LEV 3')
        self.rec.scpi.write(':TRIG:SWE NORM')
        self.rec.run(self.frames, capture_metadata=True)
        try:
            if stop:
                self.rec.scpi.write(':RECord:WRECord:OPERate STOP')
                result = expect_error(MetadataError, lambda: self.rec.wait_recorded(timeout=2), 'frame count')
            else:
                result = expect_error(ScopeRunTimeout, lambda: self.rec.wait_recorded(timeout=.5))
            expect_error(MetadataError, lambda: self.rec.read_capture(count=self.frames))
            self.clean()
            return result
        finally:
            self.rec.scpi.stop_record()

    def independence(self):
        old = Capture.load(self.archive_dir/'metadata-baseline.npz')
        ch = next(iter(old.samples))
        volts, axis = old.to_volts(ch).copy(), old.time_axis(ch).copy()
        self.configure(probe=10, rate=50e6)
        require(np.array_equal(old.to_volts(ch), volts), 'saved volts depend on live settings')
        require(np.array_equal(old.time_axis(ch), axis), 'saved time axis depends on live settings')
        self.record()  # A valid acquisition still succeeds after fault-path exercises.
        return True, 'saved conversion unchanged; fresh acquisition succeeds after rejected records'


def run_metadata_checks(rec, args, v, enabled, sources, setup):
    print('\nmetadata captures and measurement scaling:', flush=True)
    with tempfile.TemporaryDirectory(prefix='fastrec-validation-') as temp:
        checks = CaptureChecks(rec, args, v, enabled, sources, setup, v.output_dir or temp)
        if not v.run('metadata record + NPZ round trip', checks.initial):
            v.skip('remaining metadata cases', 'initial metadata acquisition failed')
            return
        v.run('actual settings, completed depth and provenance', checks.provenance)
        v.run('metadata defaults and legacy read equivalence', checks.arrays)
        def subset():
            cap = rec.read_capture(count=4, channels=[enabled[0]])
            require(set(cap.samples) == {enabled[0]}, 'single-channel result is not a mapping')
            require(np.array_equal(cap.samples[enabled[0]], checks.cap.samples[enabled[0]][:4]),
                    'selected frames differ')
            checks.archive(cap, 'subset')
            return True, 'one channel, first four frames, metadata and raw arrays preserved'
        v.run('single-channel / partial-record archive', subset)
        for label, kw in [('crop-raw', {}), ('crop-packed', {'transport':'packed'}),
                          ('crop-8bit', {'sample_bits':8}), ('crop-average', {'average':2})]:
            v.run(label+' archive / time axis', lambda label=label, kw=kw: checks.encoding(label, **kw))
        v.run('saved SCPI preamble time axis', checks.preamble_axis)
        v.run('archive refuses overwrite', lambda: expect_error(FileExistsError,
              lambda: checks.cap.save(checks.archive_dir/'metadata-baseline.npz')))
        v.run('unknown-channel scaling rejected', lambda: expect_error(
            ScalingError, lambda: rec.to_volts(np.array([32768], dtype=np.uint16), 9)))
        for label, kw in [('too many frames', {'count':9}), ('partial average', {'count':7,'average':2}),
                          ('bad crop', {'count':8,'crop':(0,10001)}),
                          ('duplicate channels', {'count':8,'channels':[enabled[0],enabled[0]]})]:
            v.run(label+' rejected', lambda kw=kw: expect_error(ValueError, lambda: rec.read_capture(**kw)))
        if sources:
            for ch in checks.freqs:
                v.run(f'CH{ch} absolute volts / DC / actual sample interval',
                      lambda ch=ch: checks.physical(checks.cap, ch))
            v.run('50-ohm capture and AFG load agreement', lambda: checks.impedance_and_probe(50, 1.))
            v.run('10x probe and nonzero vertical offset scaling', lambda: checks.impedance_and_probe(1e6, 10.))
            v.run('implicit trigger channel at 50 ohms', checks.implicit_trigger)
            if len(sources) == 2:
                v.run('noncontiguous four-lane mapping without recabling', checks.noncontiguous)
            else:
                v.skip('noncontiguous dual-AFG check', 'requires two connected AFG outputs')
            v.run('NORM sweep captures real AFG edges', checks.normal_trigger)
            v.run('inversion and deskew metadata / voltage polarity', checks.inverted)
        else:
            v.skip('absolute scaling / impedance / dual-AFG checks', '--no-afg')
        v.run('non-voltage units cannot be converted as volts', checks.nonvoltage)
        v.run('reconfiguration invalidates old record', checks.stale)
        v.run('changed settings at record completion rejected', lambda: checks.settings_drift('during_record'))
        v.run('changed settings before readback rejected', lambda: checks.settings_drift('before_read'))
        v.run('interrupted record rejected', lambda: checks.interrupted(stop=True))
        v.run('no-trigger timeout invalidates record', lambda: checks.interrupted(stop=False))
        v.run('archive independence and recovery after faults', checks.independence)
        if args.stream:
            v.run('stream invalidates saved-record association', checks.stream_invalidates)
        def error_queue():
            errors = scpi_errors(rec)
            return not errors, str(errors) if errors else 'no pending SCPI errors'
        v.run('metadata SCPI error queue clean', error_queue)
        if not getattr(args, 'csv', False):
            v.skip('same-record Rigol CSV comparison', 'enable --csv with ADB available')
