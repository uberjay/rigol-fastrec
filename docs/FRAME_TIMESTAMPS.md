# WaveRecord frame timestamps

The MHO98 records elapsed time for each WaveRecord frame, relative to the first
frame. `:RECord:WREPlay:FCURrent:TIME?` exposes the selected frame's timestamp.
A Frida observer can also read the full integer value before SCPI formats it.

## Measured behavior

Bench run on 2026-09-18 (2026-09-19 UTC), MHO98 firmware `00.01.00`, AFG1 → CH1,
three enabled channels, 1,000 samples per frame at 1 GS/s, NORM edge trigger:

| AFG setting | Frames | Expected interval | Measured interval range |
|---|---:|---:|---:|
| 2 Hz | 8 | 500 ms | 500.00000875–500.000016 ms |
| 137 Hz | 16 | 7.299270073 ms | 7.299268–7.299272 ms |
| 13.7 kHz | 32 | 72.99270073 µs | 72.98875–72.99925 µs |
| 20 Hz, interrupted output | 24 | 50 ms, plus a gap | Normal intervals 49.99999675–50.00000325 ms; one 732.57154875 ms gap |

The interrupted test switched the AFG output off for about 654 ms during one
recording. Frames 4 and 5 are separated by 732.572 ms; subsequent frames return
to 50 ms spacing. This distinguishes recorded event times from a frame index
multiplied by the configured period. The output switching commands and the AFG's
restart phase also contribute to the interval surrounding the gap.

All **34 experiment checks passed**, including zero time at frame 1, increasing
timestamps, stable first-frame origin, identical values when frames are reread
out of order, SCPI/native agreement, AFG periods, the inserted gap, unchanged raw
waveforms after timestamp collection, and clean SCPI error queues. Both AFG
outputs were confirmed off at exit.

## SCPI refresh and formatting

Selecting a frame updates `FCURrent?` immediately but schedules the timestamp
refresh on a **300 ms firmware timer**. An immediate `TIME?` query can therefore
return the preceding selection's time, even with playback paused. In the bench
run the observer saw fresh values roughly 325–335 ms after sending the selection.

For a SCPI-only client:

```text
:RECord:WREPlay:OPERate STOP
:RECord:WREPlay:FCURrent 16
# Wait 0.5 s to allow for the asynchronous 300 ms refresh.
:RECord:WREPlay:FCURrent:TIME?
# -> 109.48ms
```

The response has five significant digits and a unit suffix. In this example,
the native value is **109.489048 ms**, so the formatted response loses 9.048 µs.
Compute short inter-frame intervals from the integer values, especially late in
a long record where the display's rounding becomes large relative to an interval.

The probe waits for a new native timestamp-update event for the requested frame,
then verifies the selected frame and cache value around the SCPI query. This
avoids relying on a fixed sleep. It takes roughly half a second per frame with
network/query overhead; collecting a million frames this way would be impractical.

## Native path

The following exported functions and fields are in `libscope-auklet.so` for
MHO98 `00.01.00`:

| Function | Behavior |
|---|---|
| `CApiRecord::ApiRecord_SetPlayCurrent(int)` | Sets the current frame, starts replay of that frame, schedules timer 3 for 300 ms |
| `CApiRecord::onTimeout(int)` | Timer 3 calls `ApiRecord_UpdateTimeStamp()` |
| `CApiRecord::ApiRecord_UpdateTimeStamp()` | Reads `DrvRecord_GetTag()`, subtracts the first-frame tag and stores elapsed time; frame 1 is zero |
| `CApiRecord::ApiRecord_GetTimeStamp(RString&)` | Formats the cached elapsed value into the SCPI/display string |
| `DrvRecord_GetTag(uint64_t&)` | Reads the hardware timestamp through `DevSystemSCU_getTimeStamp()` |

`CApiRecord` fields observed by the probe:

| Offset | Type | Meaning |
|---|---|---|
| `0x90` | uint32 | Current frame, 1-based |
| `0xa8` | uint32 | Record state |
| `0xd8` | uint64 | Elapsed time in femtoseconds |
| `0xe0` | uint64 | First-frame hardware tag |

The firmware computes:

```text
elapsed_fs = (current_tag - first_tag) * 250000
elapsed_seconds = elapsed_fs / 1e15
```

The hardware tag unit is therefore **250 ps**. The observed interval spread at
13.7 kHz was **10.5 ns**. Keep the counter unit and measured timing variation
separate when using these values for jitter analysis.

The observer hooks the update and getter, taking `this` from their actual calls.
It reads the fields above and returns uint64 values as decimal strings to retain
integer precision through Frida's JSON transport. Host-side subtraction uses
Python integers before converting an interval to seconds. The scope's existing
record/replay code selects frames and refreshes timestamps.

## Repeating the experiment

Use the same AFG1 → CH1 cable as the main hardware validation. The script owns
both AFG outputs for its duration and switches them off on exit.

```bash
PYTHONPATH=python python -m tools.diagnostics.frame_timestamps \
  --host 10.0.10.213 --output-dir /tmp/frame-times
```

The output directory must be new. It receives `report.json` with every timestamp,
repeat read, interval, refresh wait, check and cleanup result, plus one raw NPZ
capture per test case. The four cases take about 90 seconds.

A [raw readback experiment](RAW_TIMESTAMPS.md) now collects the full hardware
tag directly in the agent, without this per-frame timer.

The public API now supports `read(..., timestamps=True)` and
`read_capture(..., timestamps=True)`, with exact uint64 counters and NPZ storage.
See [CAPTURES.md](CAPTURES.md#optional-frame-timestamps). The standalone observer
above remains a reference for comparing native readback against SCPI/display
values.
