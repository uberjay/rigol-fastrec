#!/usr/bin/env python3
"""Validate the opt-in timestamp API on an MHO98, AFG1 -> CH1.

Compares every tag with single-frame register readback, covers channel strides,
crops/encodings/subsets, saved captures, deliberate trigger gaps and default
averaging. Both AFG outputs are turned off on exit. Writes a new evidence folder.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time

import numpy as np
from rigol_fastrec import Capture, Channel, Trigger, WaveRecorder
from ._support.timestamps import register_reference
from ._support.bench import afg_setup, managed_afg, Validator, scpi_errors, evidence_metadata


def main(args):
    out = Path(args.output_dir); out.mkdir(parents=True, exist_ok=False)
    report = dict(started=datetime.now(timezone.utc).isoformat(), cases=[], checks=[])
    report.update(evidence_metadata(args))
    cleanup = Validator()
    def save():
        (out/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    def check(name, passed, detail=''):
        report['checks'].append(dict(name=name, passed=bool(passed), detail=detail)); save()
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}", flush=True)
        if not passed:
            raise AssertionError(name)
    def same(a,b):
        return set(a)==set(b) and all(np.array_equal(a[ch],b[ch]) for ch in a)
    cases = [([1,2,3],1000,600,False), ([1],10000,64,False),
             ([1,3],10000,64,False), ([1,2,3],10000,64,False),
             ([1,2,3],100000,64,False), ([1,2,3],1000000,8,False),
             ([1,2,3],1000,24,True)]
    try:
        with WaveRecorder(args.host) as rec, managed_afg(rec,cleanup,
                [(':SOURce1',1,13700),(':SOURce2',3,0)]):
            report['idn'] = rec.scpi.query('*IDN?')
            for channels,samples,count,gap in cases:
                label = '-'.join(map(str,channels))+f'-{samples}'+('-gap' if gap else '')
                print('CASE',label,flush=True)
                rec.configure(samples=samples,sample_rate=1e9,
                    trigger=Trigger(source='CHAN1',level=.2,channel_impedance=1e6),
                    channels={ch:Channel(range=4.,impedance=1e6) for ch in channels})
                rec.scpi.write(':TRIG:SWE NORM')
                errors = afg_setup(rec,prefix=':SOURce1',freq=20. if gap else 13700.,
                                   vpp=1.,offset=0.,wave='SQU')
                if errors: raise RuntimeError(errors)
                if gap: rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                rec.run(count,capture_metadata=True)
                if gap:
                    rec.scpi.write(':SOURce1:OUTPut:STATe ON'); time.sleep(.22)
                    rec.scpi.write(':SOURce1:OUTPut:STATe OFF'); time.sleep(.65)
                    rec.scpi.write(':SOURce1:OUTPut:STATe ON')
                rec.wait_recorded(20); rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                base = rec.read_capture(count=count)
                values,refs = register_reference(rec,count,samples,channels)
                check(label+' register samples',same(values,base.samples))
                case = dict(label=label,channels=channels,samples=samples,count=count,
                            reference_ticks=refs,reads=[])
                report['cases'].append(case)
                common = dict(count=count,samples_per_frame=samples,channels=channels)
                def read(enabled, **opts):
                    begin = time.monotonic()
                    data = rec.readback.read(**(common|opts),timestamps=enabled)
                    stats = rec.readback.last_read_stats
                    ts = rec.readback.last_frame_timestamps
                    case['reads'].append(dict(timestamps=enabled,options=opts,
                        host_s=time.monotonic()-begin,stats=stats))
                    first=opts.get('first',0);n=opts.get('count',count)
                    if enabled:
                        check(label+' exact timestamp vector',ts is not None and ts.first_frame==first
                              and ts.ticks.tolist()==refs[first:first+n])
                    else:
                        check(label+' default has no timestamp data/pass',ts is None
                              and not any(k.startswith('timestamp') for k in stats))
                    return data
                for _ in range(args.repeats):
                    normal=read(False); timed=read(True)
                    check(label+' repeated waveform identity',same(normal,timed) and same(timed,base.samples))
                for options in [dict(crop=(7,samples-11)),dict(transport='packed'),
                                dict(sample_bits=8),dict(first=1,count=count-2),
                                dict(first=count-1,count=1)]:
                    normal=read(False,**options);timed=read(True,**options)
                    check(label+' transformed waveform identity',same(normal,timed),str(options))
                cap=rec.read_capture(count=count,timestamps=True)
                path=out/(label+'.npz');cap.save(path);loaded=Capture.load(path)
                check(label+' timestamp archive',loaded.timestamps.ticks.tolist()==refs
                      and same(loaded.samples,base.samples))
                old=rec.read_capture(count=count)
                check(label+' original record restored',old.timestamps is None and same(old.samples,base.samples))
                try: rec.read_capture(count=count,average=2,timestamps=True)
                except ValueError as exc:
                    check(label+' averaging rejected', 'mutually exclusive' in str(exc))
                else: raise AssertionError('averaging with timestamps accepted')
                avg=rec.readback.read(**common,average=2)
                expected={ch:base.samples[ch].reshape(count//2,2,samples).mean(axis=1,dtype=np.float32)
                          for ch in channels}
                check(label+' ordinary averaging unchanged',same(avg,expected)
                      and rec.readback.last_frame_timestamps is None
                      and not any(k.startswith('timestamp') for k in rec.readback.last_read_stats))
                if gap:
                    intervals=cap.timestamps.intervals_seconds().tolist();case['intervals_s']=intervals
                    check(label+' recorded gap',sum(v>.6 for v in intervals)==1,str(intervals))
                errors=scpi_errors(rec);check(label+' SCPI errors',not errors,str(errors))
                medians={str(flag):statistics.median(r['host_s'] for r in case['reads']
                            if r['timestamps']==flag and not r['options']) for flag in (False,True)}
                case['host_median_s']=medians
                print('MEDIAN',label,medians,flush=True);save()
            # Match the user's 1000-acquisition averaging groups: no tags or
            # second replay pass, and the hardware's normal chunk size is kept.
            rec.configure(samples=1000,sample_rate=1e9,
                trigger=Trigger(source='CHAN1',level=.2,channel_impedance=1e6),
                channels={ch:Channel(range=4.,impedance=1e6) for ch in (1,3)})
            errors=afg_setup(rec,prefix=':SOURce1',freq=13700.,vpp=1.,offset=0.,wave='SQU')
            if errors:raise RuntimeError(errors)
            rec.run(10000);rec.wait_recorded(20);rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
            t=time.monotonic();data=rec.read(count=10000,channels=[1,3],average=1000)
            report['large_average']=dict(host_s=time.monotonic()-t,stats=rec.readback.last_read_stats)
            stats=rec.readback.last_read_stats
            check('1000-acquisition averages keep normal bulk path',
                  all(a.shape==(10,1000) and a.dtype==np.float32 for a in data.values())
                  and rec.readback.last_frame_timestamps is None and stats['chunk']>1
                  and not any(k.startswith('timestamp') for k in stats),str(report['large_average']))
            check('final SCPI errors',not scpi_errors(rec))
        check('AFG cleanup',cleanup.failed==0)
    except BaseException as exc:
        report['error']=repr(exc);raise
    finally:
        report['cleanup']=cleanup.report
        report['finished']=datetime.now(timezone.utc).isoformat();save()
    return 0


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',default='10.0.10.213')
    p.add_argument('--output-dir',required=True)
    p.add_argument('--repeats',type=int,default=4)
    args=p.parse_args()
    if args.repeats<1:p.error('--repeats must be positive')
    raise SystemExit(main(args))
