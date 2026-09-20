# WaveRecord readiness and stale playback positions

`run()` waits for both `:RECord:WREPlay:FCURrent? == 1` and
`:RECord:WREPlay:FMAX? == 1`, with DUT triggers held quiet. It does **not**
require observing a zero: the first SCPI query can block long enough to miss
that part of startup.

## Why the cursor alone was insufficient

The original failure returned a playback cursor of 3 or 4 immediately after
`RUN`. Requiring exactly 1 fixed that case, but later captures exposed a stale
**1** as well. Two logged failing batches accepted 1 on their first query and
finished 719 and 809 frames short of the requested 253000. Successful batches
usually showed `0 → 1` or `3 → 0 → 1` before releasing the DUT.

Ghidra analysis of `libscope-auklet.so` explains the difference. Addresses below
are Ghidra virtual addresses with image base `0x00100000`, from the
`/1.0.0.26/libscope-auklet.so` project program:

| Function | Address | Relevant behavior |
| --- | --- | --- |
| `CApiRecord::ApiRecord_SetRecording` | `0x006d90ac` | STOP sets the playback cursor to 1 when record progress is nonzero. RUN clears progress at `this+0x68`, but initially leaves the cursor at `this+0x90` unchanged. |
| `CApiRecord::ApiRecord_GetPlayCurrent` | `0x006da21c` | Reads `this+0x90`. |
| `CApiRecord::ApiRecord_GetRecordFrames` | `0x006d98ec` | Reads `this+0x68`. |
| `CApiRecord::on_record_timeout` | `0x006dc040` | The 100 ms timer queries driver progress (`0x42`), updates `this+0x68`, then copies it to the cursor at `this+0x90`. |
| `CApiRecord::ApiRecord_GetMaxFrames` | `0x006d8f50` | Queries driver capacity (`0x41`), a separate value. |
| `CDrvScope::SetState` | `0x003e64b0` | Record state `0xb` resets the driver's record count, current position and base before configuring and running the scope. |

The SCPI registration in `table_RECord` (`0x009e4884`) separates three queries:

| Query | API ID | Meaning |
| --- | --- | --- |
| `WREPlay:FCURrent?` | `0x3f06` | Playback cursor; may still be from the previous recording. |
| `WREPlay:FMAX?` | `0x3f23` | Record progress exposed as playback depth; cleared by RUN. |
| `WRECord:FMAX?` | `0x3f09` | Maximum recordable frames at this geometry; **not** an arming signal. |

The older `/1.0.0.25/Sparrow.apk/libscope-auklet.so` has the same relevant
behavior: `ApiRecord_SetRecording` at `0x006d5304`, `GetRecordFrames` at
`0x006d5b60`, and `on_record_timeout` at `0x006d82b4`. Its STOP path briefly
selects frame 2, then returns to frame 1; the final stale-1 problem is the same.
The scope's `*IDN?` reports `00.01.00` and does not distinguish these builds.

Waiting for zero **before** RUN is unsuitable: STOP deliberately leaves a
cursor of 1. Requiring zero **after** RUN risks a timeout if polling misses
that transient value. Checking playback FMAX alongside the cursor avoids both
problems. The older `capture_rigol.py` already used playback FMAX for arming.

## Live observations, 2026-09-20

On an MHO98 reporting firmware `00.01.00`, with NORM sweep, CHAN2 positive-edge
trigger at 1.5 V, 1000 samples/frame at 1 GSa/s, and 253000 frames per arm:

- All 12 arms completed with no DUT triggers. Four deliberately delayed the
  first readiness query by 1.5 seconds; both queries returned 1 immediately,
  without observing a zero.
- The immediate-poll trials directly reproduced the stale-cursor race. For
  example, after RUN: `FCURrent=1`, `FMAX=0`, `FCURrent=0`, `FCURrent=1`,
  `FMAX=1`. The added check held off trigger release at the first 1.
- A full `capture_fastrec.py` run then completed 20 consecutive batches of
  253000 raw frames: 5,060,000 frames total, averaged in groups of 1000 into
  5060 stored traces. It used `efr32_hwaes_200k.json`, `--trace-probe 10`,
  `delay_iters=60`, crop `0:300`, and the unchanged 10-second completion
  timeout. Both stale-1 observations during that run were rejected, and all
  batches completed with successful AES-chain checks and readback. No retries,
  skipped batches, or completion timeouts occurred.

The capture command, from `faultinjection/scaaml_efr32`, was:

```sh
python capture_fastrec.py --config config/efr32_hwaes_200k.json \
  --no-cnc --trace-probe 10 --traces 5060 --skip-test \
  --output-path /tmp/rigol-readiness-dataset -vv
```

Offline validation: all 212 Python tests passed, including regression cases
for a stale 1, a missed zero, stale larger positions, and SCPI read timeouts.

Example from the first arm (times are local):

```text
01:14:08.773  WREPlay:FCURrent? -> 1
01:14:08.801  WREPlay:FMAX?    -> 0   # keep waiting
01:14:08.840  WREPlay:FCURrent? -> 0
01:14:08.880  WREPlay:FCURrent? -> 1
01:14:08.908  WREPlay:FMAX?    -> 1   # ready
```

The pre-trigger value of 1 is an observed behavior of this firmware, not a
general SCPI guarantee or proof that one DUT frame has been captured. Neither
query should be used as an exact captured-frame counter during startup. This
handshake assumes a quiet trigger source until `run()` returns; it is not an
arming protocol for an independently running trigger source.
