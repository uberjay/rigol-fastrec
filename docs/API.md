# API: `WaveRecorder`

`WaveRecorder` is the host-side facade: it owns the SCPI control channel and the
Frida readback agent, and drives the capture loop

```
configure() → run(n) → [fire N triggers] → wait_recorded() → read()
```

`run()` and `read()` are separate so the application controls trigger timing in
between. For open-ended capture, [`stream()`](#stream-channelnone-channelsnone-cropnone-sample_bits16-transportraw-batch0)
replaces that loop: the agent arms and reads batches itself and yields one frame
per trigger until you stop iterating. The built-in Ethernet port is 100 MbE
(~11.7 MB/s) and is the bottleneck for raw reads, so most `read()` options come
down to sending fewer or denser bytes. Relative speeds below assume the link is
saturated. (For the underlying `ScpiControl`/`Readback` layers, use `rec.scpi` /
`rec.readback`.)

## Lifecycle

```python
WaveRecorder(host, *, data_port=5028, frida_port=27042)
```

Use it as a context manager -- `__enter__` opens the SCPI socket (and runs the
`*IDN?` firmware check), then attaches Frida and resolves the agent against the
firmware whitelist. `__exit__` stops any record and tears everything down:

```python
with WaveRecorder(host="10.0.80.80") as rec:
    ...
```

Raises `ScopeNotFound` (can't reach the scope / frida-server) or
`UnsupportedFirmware` (model/firmware not whitelisted) on entry.

## `configure(*, samples, sample_rate, trigger, trigger_offset_us=0.0, channels=None)`

Sets the full scope state and enters WaveRecord mode. Call once per capture
setup. Returns `None`.

| option | what it does | effect |
|---|---|---|
| `samples` | per-channel sample count (`:ACQ:MDEP`), snapped up to a valid Rigol depth | deeper means more samples/frame but fewer frames per record (`FMAX` shrinks as memory ÷ depth) |
| `sample_rate` | Sa/s; the captured window is `samples / sample_rate` | sets time resolution and window length |
| `trigger` | a `Trigger` (edge source/level/slope + its channel's vertical settings) | the trigger source channel is enabled implicitly |
| `trigger_offset_us` | signed window placement | `<0` pre-trigger, `>0` delayed |
| `channels` | `{n: Channel(...)}` to enable + their vertical settings | channels not listed are disabled, so the enabled count is fixed, and with it the FPGA interleave stride (1→2→4) and engine-frame size |

`configure()` also probes and caches `FMAX` (the max recordable frames at this
depth). To capture more than one record holds, split across multiple
`run()/read()` cycles (re-firing triggers each time) or use a shallower depth.
The actual memory depth is queried after configuration; requested sample rate
and depth need not be the settings the scope ultimately uses. The metadata
capture API below preserves both requested and actual values.

## `run(n_frames, *, ready_timeout=None, capture_metadata=False)`

Arms WaveRecord for `n_frames` and blocks until the engine is actually recording
(`:WREPlay:FCURrent` reaches 1, before any trigger), so triggers fired after
`run()` returns are caught. Fire your DUT triggers after this returns.

Raises `ValueError` immediately if `n_frames` exceeds the cached `FMAX`, and
`ScopeRunTimeout` if the engine never comes up within `ready_timeout` (default
scales with `n_frames`).

Set `capture_metadata=True` to query acquisition settings before arming and
check them again after `wait_recorded()`. This enables `read_capture()`; it adds
SCPI queries outside active recording. The default array-only path is unchanged.

## `wait_recorded(timeout=10.0)`

Blocks until the record completes, then transitions the scope to replay so the
frames are readable. Raises `ScopeRunTimeout` if the record never finishes within
`timeout` (usually means too few triggers were fired).

## `read(*, count, channel=None, channels=None, crop=None, average=1, sample_bits=16, transport="raw", progress=None, timestamps=False)`

Streams `count` recorded frames back. `channel=N` (or `channels=[N]`) returns a
bare ndarray; `channels=[a, b]` returns `{ch: ndarray}`; by default reads the
trace channels (all enabled minus the trigger source). `progress(done, total)` is
called periodically as rows arrive (throttled to ~100 updates over the read,
always firing on the last); `total = count // average` is the row count (one per
averaging group, or one per frame when `average == 1`). It runs in the reader
thread, so keep it cheap; a slow callback backpressures the readback.

| option | what it does | effect on wire / precision |
|---|---|---|
| `count=n` | number of recorded frames to read back | with `average=k`, yields `n // k` rows |
| `channel=` / `channels=` | one channel (bare array) or several (`{ch: array}`), deinterleaved in a single DMA pass | reading K channels ships K× the records |
| `crop=(lo, hi)` | in-agent per-channel sample window | fewer samples/trace, proportionally less wire, no precision loss. Raises `ValueError` if `hi > samples`. |
| `average=k` | mean of every `k` frames on the scope → float32 | collapses `k` frames into one row (`n/k` rows). Saves bandwidth and reduces noise (effective bits beyond the ADC). Always float32, so `sample_bits`/`transport` don't apply. |
| `sample_bits` | `16` → uint16, `8` → uint8 (top byte) | resolution vs wire width (below) |
| `transport` | `"raw"`, or `"packed"` (16-bit only) | wire packing (below) |
| `timestamps=True` | per-frame acquisition counters in `rec.readback.last_frame_timestamps` | extra prefix replay + counter vector; requires `average=1` |

Timestamping defaults to `False`. It leaves the waveform return type unchanged.
With `True`, readback completes the normal waveform transfer, then collects frame
counters with a separate short-prefix replay inside the same RPC. Progress reports
waveform rows; the call returns after timestamp collection also finishes. The
10 µs replay pacing applies only to this optional pass, after acquisition.
`timestamps=True, average>1` raises `ValueError` before any scope I/O. Default
reads retain normal DMA chunks and perform no timestamp replay or transport.

```python
frames = rec.read(count=600, channels=[1, 3], timestamps=True)
times = rec.readback.last_frame_timestamps
elapsed = times.relative_seconds()    # one time per frame; first is zero
intervals = times.intervals_seconds() # N-1 inter-frame intervals
```

`FrameTimestamps.ticks` is an immutable uint64 vector with `tick_fs=250000`
(250 ps per tick), shared by all returned channels. The helper methods subtract
integer counters before converting to seconds. A default read, rejected/failed
read, or closing readback clears `last_frame_timestamps`. Save the returned
object if it needs to outlive another read. `Capture.timestamps` does this for you.

For nonzero frame subsets use `rec.readback.read(first=..., count=...,
samples_per_frame=..., channels=[...], timestamps=True)`. The timestamps object's
`first_frame` preserves that zero-based index; relative times start at the first
returned frame. Cropping and sample encodings leave frame times unchanged.

### Wire encodings (raw reads, `average == 1`)

| `sample_bits` | `transport` | bytes/sample | returns | precision | rel. speed |
|---|---|---|---|---|---|
| 16 | `raw` *(default)* | 2.0 | uint16 | full 16-bit code | 1.0× |
| 16 | `packed` | 1.5 | uint16 | top 12 bits (may drop real data, see below) | ~1.33× |
| 8 | `raw` | 1.0 | uint8 | top 8 bits | ~2× |
| 8 | `packed` | n/a | n/a | n/a | *raises* |

- `16/packed` keeps the top 12 bits (drops the low 4), packed 2 samples per 3
  bytes and rebuilt to uint16 on the host with the low 4 bits zeroed -- drop-in
  with `to_volts()`. This can discard real data. It's lossless only when the
  capture's effective resolution is ≤ 12 bits, which holds for a plain
  single-shot acquisition on the 12-bit ADC (the low bits there are sub-LSB
  calibration/noise). But the codes are a processed 16-bit value: anything that
  yields more than 12 effective bits (high-res/ERES, hardware averaging, other
  DSP, depending on model and acquisition mode) puts real signal in those low
  bits, and packing throws it away. Use it when you know your capture is ≤ 12
  effective bits; otherwise stick with `16/raw`.
- `8/raw` keeps the high byte (`code >> 8`) as uint8, half resolution.
  `to_volts()` handles it (lifts uint8 back to the 16-bit domain ×256). Fine for
  SCA, where correlation is offset-invariant anyway.
- Combining `sample_bits`/`transport` with `average > 1` raises (averaged reads
  are float32 averages).

Measured on the MHO98 over the 100 MbE port (253k frames, 1000 samples):
`16/raw` 5594 frames/s, `16/packed` 7346 (1.31×), `8/raw` 10777 (1.93×), all at
the saturated ~11.7 MB/s, so the win shows up as more frames through the same
pipe. (The engine→agent DMA itself runs at ~470 MB/s; the wire, never the scope,
is the limit.)

#### Picking a combo

- Max fidelity: `16/raw` (default).
- Cheap 25%: `16/packed`, but only when your capture is ≤ 12 effective bits (see
  the caveat above).
- 2× and 8 bits is enough: `sample_bits=8`.
- Many repeats per trace: `average=k`, usually the biggest saver, and it improves
  SNR. Pair with `crop=` to send only the window you care about.

## `read_capture(*, count, channels=None, crop=None, average=1, sample_bits=16, transport="raw", progress=None, timestamps=False)`

Requires `run(capture_metadata=True)` and successful `wait_recorded()`. Returns
a `Capture` binding a channel-to-array mapping to the completed acquisition's
metadata. The default includes **all enabled channels, including the trigger**;
even a single channel stays in the mapping. `count` must fit the record and
divide evenly by `average`. Other encoding/crop options match `read()`.

Use `capture.to_volts(channel)`, `capture.time_axis(channel)`,
`capture.save(path)` and `Capture.load(path)` for offline analysis. Files retain
raw arrays plus JSON metadata, refuse overwrite, and load without pickle.
Inconsistent acquisition settings or scaling raise `MetadataError`.
Set `timestamps=True` to populate `capture.timestamps` and save exact counters
in the NPZ. Timestamping and averaging are mutually exclusive. See
[CAPTURES.md](CAPTURES.md) for the schema, input impedance, time axes and
hardware checks.

## `stream(*, channel=None, channels=None, crop=None, sample_bits=16, transport="raw", batch=0)`

Continuous capture. Returns a generator that yields one frame per trigger for as
long as you iterate. The agent owns the capture loop: it arms a batch of frames
in the FPGA, waits for the hardware to record it, DMAs it into RAM, re-arms the
next batch, and then sends the batch it just read while the next one captures.
There is no `run()`/`wait_recorded()`; call it after `configure()`.

```python
rec.scpi.write(":TRIGger:SWEep AUTO")    # free-run; omit to wait for real triggers
for frame in rec.stream(channel=1):      # uint16 (1000,), one per trigger
    volts = rec.to_volts(frame, channel=1)
    ...
    if done:
        break                            # stops the agent, closes the data socket
```

`channel=N` (or `channels=[N]`) yields a bare ndarray of shape `(samples,)`;
`channels=[a, b]` yields `{ch: ndarray}`; the default is the trace channels, as
for `read()`. `crop`, `sample_bits`, and `transport` mean what they do in
`read()` and return the same dtypes. Raw encodings only: there is no `average`.

| option | what it does |
|---|---|
| `channel=` / `channels=` | one channel (bare array) or several (`{ch: array}`), deinterleaved on the scope |
| `crop=(lo, hi)` | in-agent per-channel sample window; frames come back as `(hi - lo,)` |
| `sample_bits` / `transport` | wire encoding, same table as `read()`: `16/raw` uint16, `16/packed` uint16 with the low 4 bits zero, `8/raw` uint8 |
| `batch=n` | cap on frames per FPGA capture. `0` (default) uses the hardware maximum for the current depth (`dwMaxFrameCount`, which shrinks as depth grows). The agent sizes each capture adaptively below the cap; see Batching below |

Behaviour to know about:

- **Triggering.** `configure()` sets NORM sweep, so by default the stream waits
  for real triggers and yields nothing until they fire. For a free-running
  signal, set `:TRIGger:SWEep AUTO` first (as above; the viewer example and the
  validation harness do this). Feed triggers continuously either way.
- **Batching.** The agent sizes each capture to the trigger rate. It starts at
  one frame and doubles the arm while full batches land quickly, so sparse
  triggers are delivered one at a time with no added latency and fast
  triggers fill batches (up to `batch`) that keep the wire busy. A partial
  batch is delivered after 50 ms, or after 20 ms with no new frame, so
  latency is bounded at about 50 ms. `batch=1` forces one frame per capture
  and tops out near 850 frames/s. Measured with a square-wave trigger at 1000
  samples on the default cap (`python -m tools.diagnostics.stream_batches`):

  | trigger rate | delivered |
  |---|---|
  | up to 150 Hz | 98% or more of triggers |
  | 200 Hz | about 96% |
  | 1 kHz | about 91% |
  | 10 kHz | about 5300 frames/s, the wire limit |
- **Display.** The scope's own acquisition and display are parked for the life
  of the stream (the same export bracket `read()` uses) and restored when it
  stops. A `read()` after the stream works normally.
- **Stopping.** `break`, `gen.close()`, or leaving the `with` block asks the
  agent to finish its current batch and closes the data socket. A new
  `stream()` or `read()` can follow immediately.
- **Timeouts.** The data socket has a 20 s per-receive watchdog, so a gap of
  more than 20 s between frames raises `TimeoutError` out of the generator.

Raises `ValueError` for a bad encoding combination, an out-of-range crop, or
no channels; `AgentError` if the agent refuses to start the stream;
`ReadbackShortRead` if a record's length doesn't match; `ConnectionError` if
the socket closes mid-record.

## `to_volts(codes, channel)`

Scales codes (or `float32` averages) for `channel` to volts, using the WORD-format
preamble cached at `configure()`. Vectorized over any array shape. Correct for
`16/raw`, `16/packed`, and averages directly; `uint8` (from `sample_bits=8`)
is lifted back to the 16-bit domain (×256) first.

Missing or invalid preambles and unconfigured channels raise `ScalingError`;
there is no identity-scaling fallback. This method uses the live cached scaling.
For arrays that must survive reconfiguration, use `Capture.to_volts()` instead.
Probe attenuation is already included in the preamble and is not applied twice.

## Introspection

- `max_frames(*, refresh=False)` → the max recordable frames at the current
  depth (`FMAX`). Cached at `configure()`; `refresh=True` re-probes the scope.
- `channel_layout()` → a `ChannelLayout` with the live `stride`, enabled
  channels, per-channel lane `offsets`, and `samples_per_frame` (= MDEP).
- `rec.readback.last_read_stats` → a copy of the last completed read's agent
  telemetry (actual chunk size/capacity, frame/byte counts and DMA timing), or
  `None` before a read or after a failed request. No additional scope query.
  Opt-in timestamp reads add `timestampElapsedMs`, `timestampDmaMs`,
  `timestampDmaBytes`, `timestampRetries` and `timestampFallbackFrames`.
  The ordinary DMA fields still describe waveform transfer;
  `elapsedTotalMs` includes the timestamp pass. The tick vector is in
  `last_frame_timestamps`, not duplicated in the stats dictionary.

## Value types

```python
Trigger(source="CHAN2", level=1.5, slope="POS",          # edge trigger
        channel_range=8.0, channel_offset=0.0,           # the source channel's
        channel_coupling="DC", channel_probe=1.0,
        channel_impedance=1e6)                          # ohms

Channel(range=0.5, coupling="DC", probe=1.0,             # per-channel vertical
        offset=0.0, bandwidth_limit="OFF",               # range = full-scale V
        impedance=1e6)                                 # ohms: 1e6 or 50
```

`level`/`offset` are in probe-tip volts; `range` is full-scale (8 vertical
divisions). `slope` is `"POS"`/`"NEG"`; `bandwidth_limit` is `"OFF"`/`"20M"`/
`"250M"` (model-dependent).

Input impedance is always set and verified by `configure()`; the default is
1 Mohm. Scripts that previously relied on a front-panel 50-ohm setting must now
request `impedance=50`. An explicit `Channel` overrides the trigger channel's
implicit settings.

## Errors

All subclass `RigolFastrecError`:

- `ScopeNotFound`: scope / frida-server unreachable, or process not found.
- `UnsupportedFirmware`: model/firmware not on the whitelist (host or agent).
- `MetadataError`: missing/inconsistent acquisition metadata or invalid archive.
- `ScalingError` (also a `MetadataError`): missing/invalid waveform scaling or
  conversion of a saved channel whose units are not volts.
- `ScopeRunTimeout`: WaveRecord didn't arm (`run`) or didn't finish
  (`wait_recorded`) in time.
- `ReadbackShortRead`: the readback stream desynced / came up short (`read()` or
  `stream()`).
- `AgentError`: the Frida agent reported an error (carries any structured detail
  in `.detail`).

## Rigol-owned Record CSV export

`rec.export_csv(path, *, adb="adb", adb_serial=None, timeout=90.,
max_values=1_000_000)` exports the complete metadata-bound record through the
scope's CSV writer and retrieves it using ADB. Returns `CsvExport` with path,
remote filename, SHA-256, native writer status and parsed `RecordCsv` waveforms.
Use `rigol_fastrec.csv_export.compare_record_csv(capture, export.waveform)` for a
strict same-record comparison.
See [CSV_EXPORT.md](CSV_EXPORT.md) for setup, the Frida hooks, file handling and
comparison thresholds.
