# Raw timestamps in fast readback

The optional API preserves normal waveform readback, then replays a separate
**32-sample prefix pass** to collect per-frame timestamps. The register reference
path reads `DrvRecord_GetTag(uint64_t&)` immediately after each frame's DMA,
before another replay is armed. Both return full 64-bit hardware counters in
**250 ps units**, without SCPI frame selection or the display timer.

## Optional API

`read(timestamps=True)` and `read_capture(timestamps=True)` use the short-prefix
pass. They return exact **uint64** acquisition counters in `FrameTimestamps`;
metadata captures save the array in schema 2 NPZ files. Defaults keep the normal
waveform readback and schema 1 files. Timestamping requires `average=1`.
See [API.md](API.md) for usage and [CAPTURES.md](CAPTURES.md) for storage.

Each optional timestamp chunk is anchored by full register reads of its first
and last frames. Header words 5–7 carry only the low 48 bits, which repeat every
19.55 hours. The decoder reconstructs their epoch from those full anchors and
checks frame indices, the header marker, the first tag and strict ordering.
A chunk spanning a whole 48-bit period, or an inconsistent header association,
falls back to individual prefix replays with full register reads. Malformed
headers or exhausted DMA retries raise an error. Transfer geometry is restored
before normal playback resumes, including error paths.

The prefix pass uses a 10 µs replay interval. This is a readback setting after
acquisition; it does not space the recorded triggers. Its time, DMA bytes, retries
and fallback count are reported separately in `Readback.last_read_stats`.
No timestamp pass, vector or transport is performed with `timestamps=False`.

## Integrated API validation — 2026-09-19

`python -m tools.validate_timestamps` passed **235/235 hardware checks** on MHO98
`00.01.00`: **70 timestamped reads and 7,985 exact tag comparisons** against
single-frame register readback. The matrix includes 1/2/4 interleaved lanes,
1,000 through 1,000,000 samples, multi-chunk and single-frame reads, nonzero
starting frames, crops, packed 12-bit and 8-bit transport, NPZ round trips and
repeated switches back to default readback. Every waveform matched, all SCPI
error queues were empty, and both AFG outputs were off at exit. The deliberate
20 Hz interruption produced one **691.062 ms** interval followed by 50 ms spacing.
The reported timestamp passes needed no DMA retries or fallback frames.

Host medians from four repeated reads per mode:

| Frames × samples, channels | Default read | Timestamp read |
|---|---:|---:|
| 600 × 1,000, CH1/2/3 | 316.6 ms | 329.4 ms |
| 64 × 10,000, CH1 | 116.2 ms | 118.2 ms |
| 64 × 10,000, CH1/3 | 226.3 ms | 228.0 ms |
| 64 × 10,000, CH1/2/3 | 336.8 ms | 335.6 ms |
| 64 × 100,000, CH1/2/3 | 3,273.8 ms | 3,274.1 ms |
| 8 × 1,000,000, CH1/2/3 | 4,122.5 ms | 4,124.7 ms |

The agent's timestamp pass for the 600-frame case took a median **51 ms**;
end-to-end increase was **12.8 ms**. The host can still be draining waveform data
already sent into the socket while the agent collects timestamps. This overlap
and run-to-run transfer variation explain why differences between host medians
are smaller than the measured timestamp pass, and sometimes slightly negative.
Use the separate telemetry when budgeting readback work.

A 10,000-frame, 1,000-sample CH1/CH3 read averaged in groups of 1,000 kept
**500-frame DMA chunks**, returned ten averages per channel and performed no
timestamp pass. Its first host read took 164.0 ms. Timestamp-plus-averaging
requests were rejected before I/O in both public API and agent tests.

The default averaging path was also compared with the committed agent
(`40bf48a`) using the same recorded 10,000 frames and current Python client.
Five ABBA cycles alternated committed/current agents, nine reads per attachment,
with the first read excluded as warm-up: **80 measured reads per agent**.
Median host times were **134.6 ms committed / 133.4 ms current**; ranges were
107.3–166.9 / 103.6–171.9 ms. All averages were byte-identical, all reads retained
500-frame chunks, and no timestamp metadata or replay was requested. This
comparison found no measurable default-path regression.

The offline suite passed **164 Python tests and 10 agent tests**. Synthetic tests
exercise 48-bit counter wrap, values above 2^53, frame-index wrap, long-gap
full-register fallback, malformed headers, DMA retries and error-path geometry
restoration. Archive tests check both schema versions and immutable counter data.

```bash
PYTHONPATH=python python -m tools.validate_timestamps \
  --host 10.0.10.213 --repeats 4 --output-dir /tmp/timestamp-api
```

The output directory must be new. It receives a JSON report and timestamped NPZ
captures. The scope should have AFG1 connected to CH1; the script owns both AFG
outputs and disables them on exit.

## Results

MHO98 `00.01.00`, AFG1 → CH1, bench run 2026-09-18 (2026-09-19 UTC):

| Recording | Waveforms only, median | Waveforms + register tags, median |
|---|---:|---:|
| 600 × 1,000 samples, CH1/2/3 | 315 ms | 499 ms |
| 32 × 1,000 samples, CH1 | 10.3 ms | 25.1 ms |
| 32 × 1,000 samples, CH1/3 | 15.5 ms | 25.3 ms |
| 32 × 10,000 samples, CH1/2/3 | 172 ms | 169 ms |

These are host elapsed times from three repeated reads per mode. Small timing
differences include run-to-run variation. The timestamp path uses single-frame
replay internally; normal reads retain their existing multi-frame chunks.

**72/72 hardware checks passed:**

- Every returned waveform equals normal readback, including crop, packed 12-bit,
  8-bit, averaging and nonzero starting-frame selections.
- Raw tags repeat exactly across reads and transformations.
- All 32 frames in each short recording, all 24 frames in the gap recording,
  and 13 selected frames in the 600-frame recording match the native timestamp
  used by the SCPI/display path, exactly in integer ticks.
- The 20 Hz gap test contains one long interval and returns to 50 ms spacing.
- A rejected channel request is followed by successful timestamp readback.
- Original recordings remain unchanged, SCPI error queues are empty, and both
  AFG outputs are confirmed off at exit.

The offline suite passed 130 tests; TypeScript and three agent tests passed.

## Running the experiment

```bash
PYTHONPATH=python python -m tools.diagnostics.register_timestamps \
  --host 10.0.10.213 --output-dir /tmp/readback-timestamps
```

The script configures the scope and AFG1, saves NPZ captures and a JSON report,
and owns both AFG outputs during its run. The output directory must be new.

It forwards `timestampRegisters: true` to the existing `read_frames` agent RPC.
This internal single-frame path serves as a reference for the public prefix-pass
API. Its RPC result adds:

- `frameTimestampTicks`: decimal strings, one full tag per **input frame**;
- `timestampTickFs`: `250000`;
- `timestampBits`: `64`.

Strings retain all counter bits through Frida's JSON transport. Subtract Python
integers before converting a difference to seconds. Cropping changes samples,
not frame times. The current API rejects timestamping with averaging, including
on this reference path. The original 72-check study above predates that API
choice; the repeatable reference tool now exercises only unaveraged timestamps.

## Bulk headers: useful data, inconsistent frame association

`DevAcquireSPU_TxFrmHead(0)` includes a **16-byte header** before each frame's
samples; `TxFrmHead(1)` suppresses it. The raw sample payload is identical after
removing the headers.

The header is eight little-endian uint16 words:

| Word | Observed content |
|---:|---|
| 0 | Marker `0xfa05` |
| 1–3 | Other metadata, not decoded here |
| 4 | Frame index modulo 65,536 |
| 5–7 | Timestamp bits 47–32, 31–16, 15–0, respectively |

The header timestamp usually belongs to the **preceding** frame. Header zero
can contain a stale tag; the register after DMA supplies the last frame's tag.
This correction initially matched the reference across full, partial and
reordered transfers, including 600 frames with chunk boundaries.

Repeated 10,000-sample reads exposed a race: an occasional header instead
contains its own frame's tag. Applying the one-frame correction then duplicates
one timestamp and omits its predecessor. The same stored waveform can produce
different header tags on successive replays while every sample stays identical.
This prevents using a fixed header offset as the timestamp source.

`DevSystemScu_SetRun` argument 8 (zero-based index 7) controls the replay interval
in femtoseconds. Firmware divides it by 4,000,000 to program the 4 ns interval
register. A pacing experiment compared every frame with the reference:

| Requested replay interval | Trials with a timestamp mismatch / 6 |
|---|---:|
| 10,000 fs (register rounds to zero) | 2 / 6 |
| 1 µs | 1 / 6 |
| 10 µs | 0 / 6 |
| 100 µs | 0 / 6 |
| 1 ms | 0 / 6 |

Pacing alone did not make full-depth header association reliable across the
expanded tests below. The public API uses short-prefix replay with anchored
validation and a full-register fallback.

To reproduce the header/pacing experiment:

```bash
PYTHONPATH=python python -m tools.diagnostics.replay_headers --study pacing \
  --host 10.0.10.213 --output-dir /tmp/replay-headers
```

This diagnostic exits nonzero if any timestamp or sample comparison fails.
Its report retains the raw header words and mismatches for each pacing setting.
Full-depth header extraction is kept out of the production waveform pass.

## Bulk replay investigation, 2026-09-19

The full-depth header mismatch reproduced on a fresh 32-frame recording:
4/12 reads failed with the interval register at zero, 2/12 at 1 µs, and
0/12 each at 10 µs, 100 µs and 1 ms. All sample arrays matched.

Native Frida callbacks then logged replay setup, timestamp reads and SCU/SPU
register access, including thread IDs and monotonic event times. Callbacks run
in a CModule, independently of the JavaScript lock held during DMA. The tool
also explicitly brackets DMA with the same monotonic clock; the exclusive
NativeFunction read does not appear in its interceptor event log.

| Full-depth variant | Reads with incorrect tags / 12 |
|---|---:|
| Ordinary unpaced read | 3 / 12 |
| Native tracing enabled | 5 / 12 |
| No timestamp polling before DMA | 3 / 12 |
| No early polling, tracing enabled | 4 / 12 |
| Wait 20 ms after entering export mode | 4 / 12 |
| Wait 2 ms before DMA | 2 / 12 |
| Read DMA one frame at a time | 9 / 12 |
| One frame at a time, 1 ms between reads | 12 / 12 |
| 10 µs replay interval, no early polling | 0 / 12 |

The failing traces include transfers with no calls from another thread in the
monitored replay/register functions. Removing early timestamp polling does not
remove the mismatch. Restoring the original waveform readback afterward gives
identical samples and an empty SCPI error queue.

The mismatches track **positions within the requested transfer**, not particular
stored frames: shifting the start from 0 to 1, 3 or 7 shifts the affected absolute
frame indices by the same amount. At 10,000 samples and four interleaved lanes,
the affected corrected-tag positions were 6 and 21 (zero-based). Slowing DMA
makes failures more frequent. Even 10 µs replay pacing failed once in eight
reads with 1 ms between DMA chunks; 100 µs passed those eight reads.

These observations point to backpressure affecting the relationship between
replay timestamps and header generation. They do not identify the FPGA's exact
buffering or clock-domain behavior.

### Short-prefix timestamp pass

A second pass can replay only the first **32 samples per channel** from each
frame, with headers enabled. The normal full-waveform readback stays intact.
The prefix pass applies the observed one-header delay within each chunk and
uses the final register tag for the chunk's last frame. It compares integer
48-bit header values against the full register reference modulo 2^48.

The expanded study passed **96/96 reads and 13,776 individual timestamp
comparisons**, with no DMA retries:

- 600 frames at 1,000 samples, including 250-frame chunk boundaries;
- 64 frames at 10,000 samples with one, two and four interleaved lanes;
- 64 frames at 100,000 samples, and eight frames at 1,000,000 samples;
- zero and 10 µs replay intervals, repeated reads, nonzero starting frames and
  reversed chunk order;
- every returned prefix sample equals the corresponding original sample;
- complete recordings remain unchanged, with empty SCPI error queues.

The packaged tool then passed **42/42 additional reads and 5,314 timestamp
comparisons** across those six configurations and a 20 Hz recording with one
670 ms gap. One DMA attempt returned `-3` and succeeded on retry; all final
headers, timestamps and prefix samples matched. Full recordings were unchanged,
SCPI error queues were empty and both AFG outputs were confirmed off. The offline
suite passed **139 tests**.

For the 600-frame case, the diagnostic's additional prefix pass took a median
45 ms unpaced or 51 ms at 10 µs pacing, including host RPC and data delivery.
The native sections totaled medians of 5 ms and 11 ms respectively. These are
measurements of the standalone diagnostic, not an integrated readback API.

The prefix experiment must restore the captured SPU waveform range and transfer
length before re-enabling normal playback. `DrvWaveform_ExportBack()` does not
restore those values. An initial cross-chunk experiment omitted that restoration,
hit a DMA timeout, and was followed by a temporary SCPI timeout. SCPI recovered
without a reset. The corrected expanded run above had no such failures.

The register path remains in the main agent as a diagnostic reference. The
public optional API uses prefix replay with the full-register anchors and
fallback described above; averaging and timestamping are mutually exclusive.

Reproduce the three studies (each writes a new directory):

```bash
PYTHONPATH=python python -m tools.diagnostics.replay_headers --study pacing \
  --host 10.0.10.213 --repeats 12 --output-dir /tmp/header-pacing
PYTHONPATH=python python -m tools.diagnostics.replay_headers --study trace \
  --host 10.0.10.213 --repeats 12 --output-dir /tmp/header-trace
PYTHONPATH=python python -m tools.diagnostics.replay_headers --study prefix \
  --host 10.0.10.213 --repeats 8 --output-dir /tmp/header-prefix
```

The prefix study uses the verified per-frame register path as its reference and
also includes a deliberately interrupted 20 Hz trigger train. The other two
studies use the native timestamp field underlying SCPI/display timing. A study
exits nonzero on a tag/sample mismatch; full-depth failures are retained as
investigation evidence rather than accepted timestamps.

## Ghidra verification

The Ghidra MCP decompiler and cross-reference tools were checked against the
project `/1.0.0.25/Sparrow.apk/libscope-auklet.so`. Its function addresses match
the local ELF used for the timestamp investigation, with Ghidra's `0x100000`
image base added. The project's build label and the scope's `00.01.00` SCPI
version string are recorded separately here.

| Function | Ghidra address | Finding |
|---|---|---|
| `DrvRecord_GetTag` | `0x0046be38` | Calls the native timestamp reader; returns zero unconditionally. |
| `DevSystemSCU_getTimeStamp` | `0x00385ffc` | Pulses bit 0 of `0x40b8` low/high/low, reads low word `0x40b0` and high word `0x40b4`, combines them into a uint64. |
| `CApiRecord::ApiRecord_UpdateTimeStamp` | `0x006d6fd8` | Computes `(current_tag - first_tag) * 250000` for the elapsed-time field in femtoseconds. |
| `CDrvScope::RequestNormTrace` | `0x0040b934` | Modes `0xd`/`0xe` arm one replay frame, call `GetState`, then read and store its timestamp before the caller performs DMA. |
| `CDrvScope::GetState` | `0x003e70c4` | Returns a cached state field; does not wait for FPGA completion. |
| `CDrvScope::ReadNormTrace` | `0x0040c648` | Reads header + samples for the frame range, then decodes words 5–7 of the first header as a 48-bit sample timestamp, using the same word order as this diagnostic. |
| `DrvWaveform_ExportBack` | `0x00417bc0` | Restores analog/LA processing enables and header mode only; does not restore export waveform range or length. |
| `DevSystemScu_setRecordPlay` | `0x00384d84` | Programs replay interval `0x4068` in 4 ns units and current/base/upper frame registers. |
| `DevAcquireSPU_TxFrmHead` | `0x00373248` | Controls bit 31 of SPU TX register `0x1010`. The include/suppress polarity comes from the hardware experiment. |
| `DrvTrace_PrintHeader` | `0x0040add8` | Calls header word 4 `sys_wav_index`, agreeing with the captured frame indices. |

The timestamp latch sequence and elapsed-time conversion also match the
decompiled `/1.0.0.26/libscope-auklet.so` functions. Hardware results above
remain from the installed scope firmware; no firmware update was performed.

`DrvTrace_PrintHeader` names several additional header fields. For the
little-endian uint16 array `h`, word 1 contains source (`h[1] >> 12`), frame
object (`(h[1] >> 8) & 0xf`), mode (`(h[1] >> 4) & 0xf`), plot type
(`(h[1] >> 2) & 3`), logic-analyzer flag (bit 1) and zoom flag (bit 0).
Word 3's low 14 bits are named `tx_time_cnt`. This diagnostic does not decode
the timestamp words or provide a rule for their association with a replay frame.

The wrapper's zero return value is not an I/O success check: it discards the
native reader's return value, and the native reader itself only returns the
last register-read status. Timestamp validity in this experiment is assessed
by the repeated reads and exact comparisons described above.

The remaining bulk investigation is the relationship between SCU timestamp
updates, SPU header emission and replay pacing. No additional synchronization
control was identified in the functions inspected. The per-frame register
method and the short-prefix experiment are the working paths; full-depth header
association remains sensitive to transfer conditions.
