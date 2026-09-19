# On-scope validation

The offline tests (`pytest`) cover only the host-side logic. The DMA,
deinterleave, crop, and averaging all run on the scope, so they can only be
checked against real hardware. `tools/validate_scope.py` does that.

## Verified run: 2026-09-18

MHO98 firmware `00.01.00`, AFG1 → CH1 and AFG2 → CH3: **67/67 automated
checks passed**, including six same-record Rigol CSV comparisons. Each CSV
contained eight 10,000-sample frames on three channels: **1,440,000 voltage
values** across all six exports, with no values outside the half-WORD-code
rounding tolerance. The worst difference was 0.138 WORD-code steps. All six raw
recordings were unchanged after export, and both AFG outputs were confirmed off
at exit.

The host offline suite passed **107 tests**; three agent export-lifecycle tests
and the TypeScript check also passed.

```bash
python tools/validate_scope.py --csv --output-dir /tmp/scope-validation -v
```

The default run uses 600 frames with averaging groups of 7. Actual readback
chunks were 250 frames, confirming cross-chunk accumulation was exercised;
the largest observed mean difference was 0.00335 codes. In the 1 Vpp metadata
checks, CH1/CH3 sine fits were approximately 0.987/0.986 Vpp at 1 Mohm and
0.982/0.979 Vpp at 50 ohms. Raw captures and full results are emitted by
`--output-dir`.

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
python tools/validate_scope.py --host 10.0.10.213 \
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

Metadata and measurement checks run by default as well. They use separate short
8-frame captures (10,000 samples, requested 100 MS/s), with each connected AFG
set to a 1 Vpp sine with 0.2 V DC offset: AFG1 at 1 MHz and AFG2 at 700 kHz.
The source load indication is explicitly matched to the scope input impedance.

| Check | Proves |
|---|---|
| NPZ round trip and provenance | raw arrays, actual/requested settings, preambles, identity and encoding survive saving/loading |
| Legacy versus metadata reads | same raw samples and volts, with the trigger included by default in metadata captures |
| SCPI preamble time axis | the selected reference produces the saved origin and sample spacing |
| Single-channel / partial-record archive | channel mapping and selected frame count remain correct |
| Cropped raw / packed / 8-bit / averaged archives | encodings match the same full record; crop time axes retain original sample indices |
| Absolute sine amplitude, DC and residual | voltage scaling and actual sample interval agree with the known AFG signal, independently of repeated-read consistency |
| 1 Mohm, 50 ohm, 10x probe and nonzero vertical offset | termination is applied; probe ratio is applied once; vertical offset is handled |
| Implicit trigger at 50 ohms | trigger-channel configuration and capture defaults agree |
| Gapped `{1,3,4}` layout | physical lanes remain correct without moving the default AFG cables |
| NORM sweep | actual AFG edges can complete a capture without AUTO triggering |
| Inversion, deskew and units | settings are preserved; voltage polarity is checked; AMP channels reject voltage conversion |
| Invalid request, stale record and changed settings | the API rejects unsupported or mismatched capture associations |
| Stopped record / no-trigger timeout | an interrupted 8-frame capture cannot produce a labeled complete record |
| Reconfiguration and streaming | old saved files remain independent; live record association is invalidated |
| Rigol CSV (`--csv`) | six full records match the scope writer sample-for-sample within decimal-rounding tolerance; native completion, file integrity and unchanged raw memory are checked |
| SCPI errors and cleanup | rejected commands fail checks; both owned AFG outputs are switched off and queried on exit |

The amplitude checks allow 10% + 20 mV for Vpp, 40 mV for DC and 40 mV RMS fit
residual, multiplied by the probe setting. The 10x test uses a direct cable and
a 10x scope probe setting to exercise software scaling. The deskew check verifies
that the configured value is preserved in capture metadata.

## Running it

```bash
# full default run: AFG1 → CHAN1, AFG2 → CHAN3, nothing on CHAN2; channels 1,2,3
# (4-channel FPGA mode, dual-source cross-talk, every combination, streaming)
python tools/validate_scope.py --host 10.0.10.213

# retain JSON results and sample/metadata archives in a NEW directory
python tools/validate_scope.py --host 10.0.10.213 --output-dir /tmp/scope-validation

# only the metadata/measurement checks, or only the previous groups
python tools/validate_scope.py --metadata-only --output-dir /tmp/scope-metadata
python tools/validate_scope.py --no-metadata

# self-consistency + streaming only, no cabling
python tools/validate_scope.py --host 10.0.10.213 --no-afg

# single-output AFG cabled to CHAN1
python tools/validate_scope.py --host 10.0.10.213 --afg-channel2 0

# skip the streaming checks
python tools/validate_scope.py --host 10.0.10.213 --no-stream
```

The lane-mapping cases that matter most are 3 to 4 enabled channels (4-channel
FPGA mode), especially a non-contiguous enable set, where the FPGA keeps each
channel at its physical lane and leaves a gap for the disabled one
(`chanOffsetWithinEnabled`):

```bash
# {1,2,4}: CH3 disabled → gap at lane 2, so CH4 sits at physical lane 3 (not a packed lane 2)
python tools/validate_scope.py --host 10.0.10.213 \
    --channels 1,2,4 --afg-channel 1 --afg-channel2 4
```

Re-cable the AFG to each channel in turn (and vary `--channels`; every AFG
channel must be in the enabled set) to cover the combinations you care about.
Exit code is non-zero if any check fails.

The JSON report distinguishes PASS, FAIL and SKIP, records the scope identity and
arguments, and is updated after each check. NPZ files contain the tested raw
captures. Without `--output-dir`, archive round trips use a temporary directory.
An existing output directory is refused. Unexpected exceptions and Ctrl-C are
reported as failures; AFG cleanup still runs. The scope returns to free-run with
the last test settings; the script does not restore the entire prior front-panel
configuration. No cables need to move during a default run.

### Stream batching against trigger rate

`tools/stream_batch_probe.py` measures the stream loop's adaptive batch sizing.
It drives a square wave from AFG1 into CHAN1 and triggers on it in NORM sweep,
so the trigger rate equals the AFG frequency, then streams for a few seconds per
(rate, batch) case and timestamps every frame. Frames inside one batch arrive
back to back, so arrival gaps recover the batch sizes; frames per second against
the trigger rate shows loss; identical consecutive frames would mean a stale
replay. Run it after touching `waitCaptured` or the arm-size adaptation:

```bash
python tools/stream_batch_probe.py --host 10.0.10.213
python tools/stream_batch_probe.py --host 10.0.10.213 \
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

These are now the defaults. The harness also reads `rec.readback.last_read_stats`
and records the actual chunk size/capacity in the JSON report. It explicitly
reports whether the requested frame count and averaging factor exercised a
group across a chunk boundary, instead of assuming they did. A smaller custom
run can pass its averaging comparison while that coverage is reported as SKIP.

## Same-record Rigol CSV cross-check (automated)

Use `--csv` to export all frames in six short metadata cases through Rigol's own
Record CSV writer, pull them using ADB, and compare all values with fastrec.
The check includes a raw re-read after each export to prove the acquisition
memory is unchanged. See [CSV_EXPORT.md](CSV_EXPORT.md) for the corrected save
command, temporary firmware source selection, file handling and comparison limits.

```bash
python tools/validate_scope.py --host 10.0.10.213 --csv \
    --output-dir /tmp/scope-csv-validation
```

## Firmware behavior

On MHO98 firmware `00.01.00`, `:ACQ:SRAT?` before a new record can describe the
previous acquisition. The metadata path accepts a rate refresh at completion,
while checking the configured timebase, depth and channels. The completed
record's rate must then remain fixed throughout readback. Actual and requested
rates can also differ: the 10,000-sample test requested 100 MS/s and acquired at
50 MS/s. The fit uses the acquired rate and saved files retain both values.

The same firmware rejected redundant `:CHANn:TCAL 0` writes at 20 us/div with
`-222,"Data out of range"`, despite reporting zero deskew. The harness queries
deskew first, resets it only if nonzero, and verifies zero afterward. Rejected
configuration commands fail the check.
