# On-scope validation

The offline tests (`pytest`) cover only the host-side logic. The DMA,
deinterleave, crop, and averaging all run on the scope, so they can only be
checked against real hardware. `tools/validate_scope.py` does that.

## Prerequisites

- Package installed: `pip install -e .` (the agent bundle is committed).
- Scope reachable over SCPI (`:5555`) and frida-server running on it (`:27042`).
- For the lane-mapping check: the scope's built-in AFG, with its output cabled
  to one input channel (any BNC cable, scope output → channel input). The
  defaults assume a dual-output AFG with AFG1 (`:SOURce1`) on CHAN1 and AFG2
  (`:SOURce2`) on CHAN3, and nothing on CHAN2 (the undriven cross-talk
  witness). Pass `--afg-channel2 0` for a single-output AFG.

## What it checks

Self-consistency (no signal or cabling needed; run with `--no-afg`):

| Check | Proves |
|---|---|
| raw shape/dtype | `(frames, mdep)` `uint16` |
| re-read determinism | replaying the same record is stable |
| crop == full slice | in-agent crop matches `full[:, lo:hi]` |
| average == numpy mean of raw | the on-scope accumulator (including cross-chunk groups and the stride-4 scalar path) |
| 16-bit packed == top-12 bits | `transport="packed"` unpacks to `full & 0xFFF0` |
| 8-bit == 16-bit high byte | `sample_bits=8` is `full >> 8` |
| crop + average / + packed / + 8-bit | the encodings compose correctly with crop |
| multichannel demux == per-channel | single-shot `read([a,b,c])` routes records to the right channel keys |
| multichannel + average / + crop | per-channel cross-chunk accumulators + crop in one pass |
| crop past end rejected | `ValueError` when `crop` exceeds `samples_per_frame` |
| disabled channel rejected | `AgentError` when reading a not-enabled channel |

These are strong but have one blind spot: a wrong-but-consistent deinterleave
offset would pass all of them (single- and multi-channel reads agree with each
other while both point at the wrong lane). That's what the AFG check is for.

Lane mapping (needs the AFG):

Drives a known sine into the `--afg-channel` input and checks:
- that channel's readback carries the signal and the other enabled channels stay
  quiet, so the requested channel maps to the right physical lane;
- the reconstructed frequency (rFFT) matches the AFG frequency within 5%, so the
  sample-rate/time axis and codes are right.

All of this works on a single-shot multichannel read's demuxed per-channel
arrays, so it also checks the multichannel routing.

Dual-output AFG (`--afg-channel2`): drive a second channel at a different
frequency. Each driven channel must report its own frequency, so a swap or
interleave bug (one channel's array picking up another's samples) fails directly,
because the channel reads the wrong frequency. This is the strongest cross-talk
test, and the case worth hitting is again a non-contiguous enable set:

```bash
# CH1 @ 12 MHz and CH4 @ 7 MHz, both read in one pass; {1,2,4} → 4-ch mode
python tools/validate_scope.py --host mho98.oodles.be \
    --channels 1,2,4 --afg-channel 1 --afg-channel2 4
```

Streaming (agent-driven continuous capture; skip with `--no-stream`):

| Check | Proves |
|---|---|
| stream raw shape/dtype | each yielded frame is `(mdep,)` `uint16` |
| stream re-captures (frames vary) | consecutive frames differ, so the loop re-arms instead of replaying a stale buffer |
| stream + packed / + 8-bit / + crop | the raw encodings and crop work on the stream path |
| stream multichannel demux | `stream(channels=[a,b,c])` yields `{ch: array}` with every channel |
| stream CHAN*n* reconstructs *f* | the streamed frequency matches the AFG (needs the AFG) |
| read() works after stream | export mode is restored once the stream stops |

Each stream check pulls `--stream-frames` frames (default 8) and stops. AUTO
sweep keeps triggers flowing, so no DUT is needed.

## Running it

```bash
# full default run: AFG1 → CHAN1, AFG2 → CHAN3, nothing on CHAN2; channels 1,2,3
# (4-channel FPGA mode, dual-source cross-talk, every combination, streaming)
python tools/validate_scope.py --host mho98.oodles.be

# self-consistency + streaming only, no cabling
python tools/validate_scope.py --host mho98.oodles.be --no-afg

# single-output AFG cabled to CHAN1
python tools/validate_scope.py --host mho98.oodles.be --afg-channel2 0

# skip the streaming checks
python tools/validate_scope.py --host mho98.oodles.be --no-stream
```

The lane-mapping cases that matter most are 3 to 4 enabled channels (4-channel
FPGA mode), especially a non-contiguous enable set, where the FPGA keeps each
channel at its physical lane and leaves a gap for the disabled one
(`chanOffsetWithinEnabled`):

```bash
# {1,2,4}: CH3 disabled → gap at lane 2, so CH4 sits at physical lane 3 (not a packed lane 2)
python tools/validate_scope.py --host mho98.oodles.be \
    --channels 1,2,4 --afg-channel 1 --afg-channel2 4
```

Re-cable the AFG to each channel in turn (and vary `--channels`; every AFG
channel must be in the enabled set) to cover the combinations you care about.
Exit code is non-zero if any check fails.

### Stream batching against trigger rate

`tools/stream_batch_probe.py` measures the stream loop's adaptive batch sizing.
It drives a square wave from AFG1 into CHAN1 and triggers on it in NORM sweep,
so the trigger rate equals the AFG frequency, then streams for a few seconds per
(rate, batch) case and timestamps every frame. Frames inside one batch arrive
back to back, so arrival gaps recover the batch sizes; frames per second against
the trigger rate shows loss; identical consecutive frames would mean a stale
replay. Run it after touching `waitCaptured` or the arm-size adaptation:

```bash
python tools/stream_batch_probe.py --host mho98.oodles.be
python tools/stream_batch_probe.py --host mho98.oodles.be \
    --rates 50,200,1000,10000 --batches 1,16,0
```

Expected on the MHO98 at 1000 samples with the default cap: every trigger up to
about 150 Hz, about 96% at 200 Hz, about 91% at 1 kHz, and the wire limit
(about 5300 frames/s) at 10 kHz. The probe also prints the agent's `stream poll`
telemetry (the first transitions of each stream), which shows the
`getPlayInfo` count advancing and why each batch was released (full, fill
timeout, or quiet timeout).

### First run: confirm the AFG SCPI

The AFG mnemonics vary by model and option. The script queries the SCPI error
queue after each AFG command and, if the scope rejects any, prints which command
and error. For example:

```
  ⚠ the scope REJECTED these AFG commands -- fix the mnemonics in afg_setup():
      :SOURce1:OUTPut ON  →  -113,"Undefined header"
```

The headers in `afg_setup()` follow the DHO800/900 (same as MHO900) programming
guide's `:SOURce` subsystem: amplitude is `:SOURce<n>:VOLTage:AMPLitude`, output
enable is `:SOURce<n>:OUTPut:STATe ON`. If your model differs, fix the offending
command there against your programming guide, or pass `--afg-prefix`. Running
with `-vv` also surfaces the agent's `fastrec_chunk` telemetry (chunk/stride/nch),
handy while bringing up the multichannel path.

### Tuning the averaging check

The agent sums `k` frames into an accumulator and divides, and that accumulator
persists across readback chunks (one DMA per chunk, capped at the hardware's
`dwMaxFrameCount`), because a k-group can begin in one chunk and finish in the
next. To exercise that cross-chunk carry-over, two things must hold:

1. more than one chunk: `--frames` greater than the chunk size, and
2. a k-group crosses a boundary: `--average` does not evenly divide the chunk
   size (otherwise each chunk holds whole groups and none straddle).

The real chunk size is the `chunk=`/`cap=` value in the `fastrec_chunk` telemetry
(`-vv`), clamped to `dwMaxFrameCount`, which is how many frames of the current
MDEP fit in record memory. So the cap shrinks as you deepen `--samples` (about
250 at 1000 samples, less at greater depth). To span more than one chunk, either
raise `--frames` above the cap or deepen `--samples` to lower it, then pick a
`--average` that doesn't divide it. Check the actual `cap=` for your depth. For
example, at 1000 samples (cap ~250):

```bash
--frames 600 --average 7    # chunks ~250/250/100; 7-frame groups straddle them
```

## One-time authoritative cross-check (manual)

WaveRecord frame data can't be read back over SCPI (`:WAV:DATA?` doesn't apply to
it). The only path is the scope UI's Save → CSV. To check the fast readback
against Rigol's own data once:

1. Record a batch and save one frame to `.npy`:
   ```bash
   python examples/capture_basic.py --host mho98.oodles.be --frames 1 \
       --samples 1000 --out /tmp/frame.npy
   ```
2. On the scope UI, export the same recorded frame to CSV and copy it off.
3. Compare the columns. The fast-readback codes (or `to_volts`) should match the
   CSV sample-for-sample.
