# Captures with saved metadata

For measurements that need to survive reconfiguration or disconnection, use
`run(capture_metadata=True)` followed by `read_capture()`. This opt-in path binds
the arrays to a snapshot of the acquisition settings and full waveform
preambles. Existing `read()` and `stream()` still return arrays and do not issue
the extra snapshot queries.

```python
from rigol_fastrec import Capture, Channel, Trigger, WaveRecorder

with WaveRecorder(host="10.0.80.80") as rec:
    rec.configure(samples=100000, sample_rate=100e6, trigger_offset_us=-100,
                  trigger=Trigger(source="CHAN2", level=1.5),
                  channels={1: Channel(range=8, probe=10, impedance=1e6)})
    rec.run(4, capture_metadata=True)
    # Supply four external triggers after run() returns.
    rec.wait_recorded(timeout=60)
    capture = rec.read_capture(count=4)
    capture.save("startup.npz")

# A different process, with no scope connection:
capture = Capture.load("startup.npz")
volts = capture.to_volts(1)       # (4, 100000), using the saved preamble
seconds = capture.time_axis(1)   # sample spacing from actual queried sample rate
settings = capture.metadata.to_dict()
```

See [capture_with_metadata.py](../examples/capture_with_metadata.py) for a CLI
that waits for external triggers and saves the record. It does not generate
triggers or control a DUT, AFG or supply.

## What is saved

The NPZ contains a JSON scalar named `metadata` and arrays named `ch1`, `ch2`,
etc. Schema version 1 preserves:

- Full instrument ID, model and firmware, host package version and agent SHA-256.
- Requested sample count/rate/window offset separately from actual queried
  sample rate, memory depth, timebase, acquisition mode and edge-trigger settings.
- Actual channel range, offset, coupling, bandwidth limit, probe ratio,
  input impedance, inversion, deskew and units, plus all ten preamble fields.
- Completed/read frame counts, saved channels, crop interval, averaging and
  wire encoding; host UTC times after arming and readback.

`read_capture()` defaults to **all enabled channels, including the trigger**.
This differs from legacy `read()`, whose default omits the trigger channel.
It always returns a `Capture` with a channel-to-array mapping, even for one
channel. Select a subset with `channels=[1]`.

The default is individual frames, raw 16-bit codes, without averaging. Keeping
individual frames is useful when looking for occasional faults; averaging can
hide them. Optional
`sample_bits=8`, `transport="packed"` and `average=k` follow the encoding rules
in [API.md](API.md). Unlike `read()`, `read_capture()` requires `count` to divide
evenly by `average`, so a partial group is never silently dropped. Crops use
original per-channel sample indices with an exclusive upper bound.

`Capture.save()` refuses to overwrite an existing path. `Capture.load()` needs
no instrument, disables pickle, and validates the schema, channel keys, array
shape and encoding. Raw codes are retained; voltage conversion remains separate
and uses the saved preamble even if the live scope has since been reconfigured.

## Optional frame timestamps

```python
capture = rec.read_capture(count=600, timestamps=True)  # after run + wait_recorded
capture.save("timed.npz")

loaded = Capture.load("timed.npz")
ticks = loaded.timestamps.ticks                # uint64, one per frame
frame_times = loaded.timestamps.relative_seconds()
intervals = loaded.timestamps.intervals_seconds()
```

These counters describe the recorded acquisitions, including gaps between
triggers. Each tick represents 250 ps; the epoch is the scope's hardware counter,
not UTC. All channels in a frame share one timestamp. `time_axis(channel)`
remains the sample axis within each frame.

`timestamps=False` is the default and keeps schema 1 files unchanged.
`timestamps=True` requires `average=1`: averaging combines several acquisitions
into one waveform, so it has no single frame timestamp in this API. The request
is rejected before scope I/O if both features are enabled. Crops, channel subsets
and all raw sample encodings work with timestamping. `stream()` does not collect
frame timestamps.

Timestamped captures use **schema 2**, retaining the existing metadata and channel
arrays and adding:

- `frame_timestamp_ticks`: one-dimensional uint64 array, length `read_frames`.
- `frame_timestamp_metadata`: JSON scalar with `first_frame` (zero for
  `read_capture()`), `tick_fs` (250000), and `source` (`prefix-register`).

The loader accepts both schemas and checks timestamp count, ordering and frame
identity. Counter subtraction happens in integers before conversion to seconds,
so a large counter value does not erase short inter-frame intervals. Timestamp
arrays own immutable storage and remain valid after reconfiguration or closure.

Collection adds a second replay of 32 samples per enabled channel with a 10 µs
replay interval. It runs after normal waveform transfer and does not change
acquisition timing. Default reads do none of this work. See
[RAW_TIMESTAMPS.md](RAW_TIMESTAMPS.md) for counter reconstruction and hardware
validation, and [API.md](API.md) for telemetry.

## Configuration and consistency checks

Input impedance is now explicit: `Channel(impedance=1e6)` is the default;
`Channel(impedance=50)` requests 50 ohms. `configure()` sets and verifies this
before setting the vertical range, because available ranges depend on impedance.
For an implicitly enabled trigger channel, use `Trigger(channel_impedance=...)`;
an explicit entry in `channels` takes precedence. **If an existing script relied
on a front-panel 50-ohm setting, it must now request `impedance=50`.**

Missing, malformed, nonfinite or inappropriate WORD/RAW preambles raise
`ScalingError`. A failed refresh clears the cached scaling; there is no identity
fallback. Converting an unknown channel also raises. `Capture.to_volts()` rejects
channels whose reported units are not volts. The Rigol preamble already includes
probe attenuation; the library does not multiply by the probe ratio again.

Metadata acquisition requires NORM acquisition mode. It checks settings before
arming, after completion, and before/after readback. It verifies the completed
frame count, rejects settings changes, and checks that the completed record's
voltage scaling stays fixed during readback. An interrupted or inconsistent
record raises `MetadataError` instead of producing a labeled capture. Run and
wait again to establish a new valid record.

The pre-arm sample-rate query can still describe an older acquisition. A refresh
of that derived rate at record completion is allowed while the configured
timebase/depth/channel settings must stay fixed. Saved metadata uses the completed
record's rate, which must stay fixed for the subsequent readback checks.

Use exclusive control of the instrument during the
`run → wait_recorded → read_capture` sequence. Keep settings and acquisition
memory fixed until readback completes. Metadata capture uses this finite-record
flow; `stream()` returns arrays.

Commands and scaling follow the [Rigol MHO900 Programming Guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/MHO900-ProgrammingGuide.pdf),
sections 3.3.4 (sample rate), 3.6.7 (impedance), 3.19.16 (completed replay depth)
and 3.28 (waveform preamble). See [VALIDATION.md](VALIDATION.md) for automated
hardware coverage, [Rigol CSV comparisons](CSV_EXPORT.md) and firmware behavior.

## Time axes

`capture.time_axis(channel)` returns sample times in seconds, using the actual
queried sample rate and zero at the start of the original record. For a crop
starting at sample 100, the first value is `100 / sample_rate`.

To use the saved RAW preamble's origin and interval:

```python
seconds = capture.time_axis(1, reference="scpi_preamble")
```

This computes `(sample_index - x_reference) * x_increment + x_origin` and checks
that the preamble interval agrees with the queried rate. Both modes return a
one-dimensional sample axis shared by the frames. The separate `host_ready_utc`
and `host_read_utc` fields record when the host finished arming and readback.

## Hardware checks

`python -m tools.validate_scope` automates the impedance, record integrity, AFG
scaling, gapped-channel layout and offline archive checks below. Its default
wiring is AFG1 → CH1 and AFG2 → CH3; use `--output-dir` to retain JSON results and
NPZ evidence. Add `--csv` for automatic same-record Rigol CSV export/comparison
through Frida and ADB.

The low-voltage AFG setup exercises these checks:

1. Check both impedance settings, including an implicit trigger channel, against
   the front-panel indication and SCPI error queue.
2. Complete a short record and interrupt another. Confirm completed-frame
   detection and that the snapshot queries do not disturb replay/readback.
3. Use `--csv` (or `rec.export_csv`) to export the **same recorded frames** and
   compare every sample with the saved capture's voltages. Nonzero offset and
   a non-unity probe setting are included; retain raw codes and preambles.
4. Check the CSV sample interval and common origin against the preamble, plus
   cropped reads and noncontiguous channels.
5. Reconfigure the scope, disconnect it and reload the NPZ. Confirm that voltage
   conversion and the default sample-index axis are unchanged.

The offline suite covers archives, scaling, impedance readback, actual versus
requested settings, incomplete records, settings/scaling drift, crop timing,
encodings and compatibility with legacy array reads.
