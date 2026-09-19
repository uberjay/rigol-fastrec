#!/usr/bin/env python3
"""Exercise experimental timestampRegisters in fastrec's native readback loop.

AFG1 -> CH1. AFG2 is disabled. Normal readback remains the comparison baseline.
Checks the raw-register reference against the native SCPI/display timestamp.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import time
import numpy as np
from rigol_fastrec import WaveRecorder,Channel,Trigger
from .._support.bench import afg_setup, managed_afg, Validator, scpi_errors, evidence_metadata
from .._support.timestamps import RegisterTimestampScript, ticks_from_status
from .frame_timestamps import FrameTimeObserver


def main(args):
    out = Path(args.output_dir);out.mkdir(parents=True,exist_ok=False)
    report = dict(started=datetime.now(timezone.utc).isoformat(), cases=[], checks=[])
    report.update(evidence_metadata(args))
    cleanup = Validator()
    def save(): (out/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    def check(name, ok, detail=''):
        report['checks'].append(dict(name=name,passed=bool(ok),detail=detail));save()
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}",flush=True)
    try:
        with WaveRecorder(args.host) as rec, managed_afg(rec,cleanup,[(':SOURce1',1,13700),(':SOURce2',3,0)]):
            report['idn']=rec.scpi.query('*IDN?')
            real_script=rec.readback._script
            proxy=RegisterTimestampScript(real_script);rec.readback._script=proxy
            try:
                for channels,samples,count,gap in [([1,2,3],1000,600,False),([1],1000,32,False),
                        ([1,3],1000,32,False),([1,2,3],10000,32,False),([1,2,3],1000,24,True)]:
                    label='-'.join(map(str,channels))+f'-{samples}'+('-gap' if gap else '')
                    print('CASE',label,flush=True)
                    proxy.enabled=False
                    rec.scpi.write(':SOURce2:OUTPut:STATe OFF')
                    rec.configure(samples=samples,sample_rate=1e9,
                        trigger=Trigger(source='CHAN1',level=.2,channel_impedance=1e6),
                        channels={ch:Channel(range=4.,impedance=1e6) for ch in channels})
                    for ch in channels:
                        rec.scpi.write(f':CHAN{ch}:INV 0');rec.scpi.write(f':CHAN{ch}:UNIT VOLT')
                    rec.scpi.write(':TRIG:SWE NORM')
                    rate=20. if gap else 13700.
                    errors=afg_setup(rec,prefix=':SOURce1',freq=rate,vpp=1.,offset=0.,wave='SQU')
                    if errors:raise RuntimeError(errors)
                    rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                    rec.run(count,capture_metadata=True)
                    rec.scpi.write(':SOURce1:OUTPut:STATe ON')
                    if gap:
                        time.sleep(.22);rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                        time.sleep(.65);rec.scpi.write(':SOURce1:OUTPut:STATe ON')
                    rec.wait_recorded(15);rec.scpi.write(':SOURce1:OUTPut:STATe OFF')
                    cap=rec.read_capture(count=count);cap.save(out/(label+'.npz'))
                    case=dict(label=label,frames=count,channels=channels,samples=samples,reads=[],references=[])
                    report['cases'].append(case);save()
                    common=dict(count=count,samples_per_frame=samples,channels=channels)
                    def read(label,flag,**kw):
                        proxy.enabled=flag;t=time.monotonic()
                        result=rec.readback.read(**(common|kw))
                        status=rec.readback.last_read_stats
                        row=dict(label=label,host_s=time.monotonic()-t,status=status)
                        case['reads'].append(row);save()
                        return result,status
                    base,_=read('normal',False)
                    got,status=read('timestamps',True)
                    ticks=ticks_from_status(status,0,count)
                    check(label+' samples',all(np.array_equal(base[ch],got[ch]) for ch in channels))
                    check(label+' monotonic',all(a<b for a,b in zip(ticks,ticks[1:])))
                    # Repeat in alternating modes; compare every raw timestamp.
                    for i in range(3):
                        normal,_=read('normal-repeat',False)
                        again,st=read('timestamp-repeat',True)
                        check(label+f' repeat {i}',all(np.array_equal(normal[ch],again[ch]) for ch in channels)
                              and ticks_from_status(st,0,count)==ticks)
                    for name,opts in [('crop',dict(crop=(7,samples-11))),('packed',dict(transport='packed')),
                        ('8bit',dict(sample_bits=8)),
                        ('subset',dict(first=count-3,count=3))]:
                        normal,_=read(name+'-normal',False,**opts)
                        again,st=read(name+'-timestamps',True,**opts)
                        first=opts.get('first',0);n=opts.get('count',count)
                        check(label+' '+name,all(np.array_equal(normal[ch],again[ch]) for ch in channels)
                              and ticks_from_status(st,first,n)==ticks[first:first+n])
                    # Rejected timestamp read must restore export mode/socket.
                    try:read('invalid-channel',True,channels=[8])
                    except Exception as exc:case['expected_rejection']=str(exc)
                    else:raise AssertionError('invalid channel accepted')
                    again,st=read('after-rejection',True)
                    check(label+' recovery',all(np.array_equal(base[ch],again[ch]) for ch in channels)
                          and ticks_from_status(st,0,count)==ticks)
                    proxy.enabled=False
                    rec.scpi.write(':RECord:WREPlay:OPERate STOP');time.sleep(.4)
                    ob=FrameTimeObserver(rec)
                    try:
                        frames=range(1,count+1) if count<=32 else sorted(set([1,2,count//2,count-1,count]+([249,250,251,252,499,500,501,502] if count==600 else [])))
                        for frame in frames:case['references'].append(ob.select(frame))
                    finally:ob.close()
                    check(label+' timestamp reference',all(ticks[row['frame']-1]==
                          (int(row['first_tag'])+int(row['elapsed_fs'])//250000)
                          for row in case['references']))
                    if gap:
                        intervals=[(b-a)*250e-12 for a,b in zip(ticks,ticks[1:])]
                        case['intervals_s']=intervals
                        check(label+' inserted gap',sum(dt>.6 for dt in intervals)==1,str(intervals))
                    after=rec.read_capture(count=count)
                    check(label+' original record unchanged',all(np.array_equal(cap.samples[ch],after.samples[ch]) for ch in channels))
                    errors=scpi_errors(rec);check(label+' SCPI errors',not errors,str(errors))
                    save()
            finally:rec.readback._script=real_script
        report['cleanup']=cleanup.report;check('AFG cleanup',cleanup.failed==0)
    except BaseException as exc:
        report['error']=repr(exc);raise
    finally:
        report['cleanup'] = cleanup.report
        report['finished']=datetime.now(timezone.utc).isoformat();save()
    return 0 if all(c['passed'] for c in report['checks']) else 1


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',default='10.0.10.213')
    p.add_argument('--output-dir',required=True)
    raise SystemExit(main(p.parse_args()))
