# Changelog

Notable changes to rigol-fastrec. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-09-19

### Added
- Opt-in `timestamps=True` on `read()` / `read_capture()`: exact per-frame uint64
  counters, integer-preserving relative times and intervals, and schema 2 NPZ
  storage. Mutually exclusive with averaging; default reads keep their existing
  waveform DMA path and schema 1 archives without a timestamp pass.
- Anchored 32-sample timestamp replay with 48-bit epoch reconstruction, full-register
  fallback, bounded DMA retries, geometry restoration and separate timing/byte
  telemetry. `python -m tools.validate_timestamps` exercises the public API on hardware.
- Experimental per-frame hardware timestamp reads in the agent, plus repeatable
  register and bulk-header/pacing probes. The register study passed 72 checks.
- Native Frida replay tracing and a separate 32-sample bulk timestamp experiment;
  19,090 tag comparisons passed across layouts, depths, chunk boundaries and a
  deliberate trigger gap. Documents transfer-state restoration and the remaining
  full-depth header association failure.
- `python -m tools.diagnostics.frame_timestamps` measures WaveRecord timestamps with a Frida
  observer, checks known AFG periods and an inserted gap, and archives per-frame
  integer times alongside raw captures. Documents SCPI's asynchronous refresh.
- `WaveRecorder.export_csv()` invokes Rigol's own Record CSV writer and retrieves
  its output via ADB, with one-shot firmware-gated source selection, bounded
  files/timeouts, strict parsing and no overwrite of existing files.
- `--csv` scope validation compares six complete records against Rigol CSV,
  checks every sample and verifies raw memory is unchanged afterward; saves CSV,
  NPZ, hashes and per-channel errors.
- Opt-in `run(capture_metadata=True)` / `read_capture()`: raw samples bound to
  actual acquisition settings, full preambles, encoding and provenance.
- Portable `Capture.save()` / `Capture.load()` NPZ archives with JSON metadata,
  offline voltage conversion and crop-aware time axes based on the sample rate
  or saved SCPI preamble.
- Explicit `Channel.impedance` and `Trigger.channel_impedance` (50 or 1e6 ohms),
  set and verified before vertical configuration.
- Capture example and offline tests; automated scope validation of archives,
  scaling, both impedances, probe ratio, units, gapped lanes and failure paths.
- JSON validation reports and saved NPZ evidence via `--output-dir`, with AFG
  cleanup on failures/interrupts.
- `Readback.last_read_stats` exposes successful agent readback telemetry without
  extra queries, including actual chunk size for averaging coverage checks.

### Changed
- Organized checkout tools into routine validators, `tools.diagnostics` and shared
  support modules. Run them with `python -m tools...`; `tools/README.md` documents
  the firmware revalidation workflow. Reports include configuration and source
  hashes; the stream probe now saves measurements and fails on acquisition or
  cleanup errors. Replay-header diagnostics default to the short-prefix study.
- **Input impedance defaults to 1 Mohm and is always set.** Scripts relying on
  a pre-existing 50-ohm front-panel setting must request `impedance=50`.
- Missing/invalid preambles and unknown-channel conversion raise `ScalingError`
  instead of substituting identity scaling.
- Configuration reads back actual memory depth instead of assuming the scope
  accepted the requested depth.
- Scope validation defaults to 600 frames averaged in groups of 7, and reports
  whether a group actually crossed a readback chunk boundary.

### Fixed
- Metadata accepts a sample-rate refresh at acquisition completion: the pre-arm
  SCPI query can still describe the previous record on MHO98 `00.01.00`. Rate
  changes during readback remain errors; actual versus requested rates are saved.
- Python `channel_layout().offsets` now matches the agent's physical lanes for
  noncontiguous channels in four-lane mode (for example channels 1, 2 and 4).

## [0.2.0] - 2026-09-18

### Added
- `WaveRecorder.stream()` / `Readback.stream()`: agent-driven continuous
  capture. The agent arms each FPGA batch, waits for the hardware to record it,
  replays it, and re-arms the next batch before sending, so capture and wire
  overlap. The batch size adapts to the trigger rate (one frame per capture for
  sparse triggers, up to `batch` for fast ones, latency bounded at about
  50 ms). Yields one frame per trigger until the caller stops iterating. Raw
  encodings (`sample_bits`, `transport`), `crop`, and multi-channel demux as in
  `read()`; no averaging.
- `python -m tools.diagnostics.stream_batches`: measures the stream loop's batching and
  delivered fraction against an AFG-paced trigger across rates.
- `examples/stream_viewer.py`: live pyqtgraph viewer over `stream()`, installed
  with the new `viewer` extra (`pip install -e '.[viewer]'`).
- Streaming checks in `python -m tools.validate_scope` (shape, live re-capture, each
  encoding, crop, multichannel, AFG frequency, and `read()` after a stream);
  `--no-stream` skips them.

### Changed
- `python -m tools.validate_scope` defaults now assume AFG1→CHAN1 and AFG2→CHAN3 with
  `--channels 1,2,3`, so a bare run exercises every check. Pass
  `--afg-channel2 0` for a single-output AFG.
- `pyproject.toml` moved to the repository root, so the install is
  `pip install -e .` (was `pip install -e python`). Current hatchling rejects a
  readme outside the project directory, which broke the release build.

## [0.1.0] - 2026-06-12

First public release: drive a Rigol MHO900-series scope's WaveRecord memory and
read frames back over a Frida-injected agent, with averaging, deinterleave, and
cropping done on the scope. Validated on an MHO98 (firmware `00.01.00`).

### Added
- `WaveRecorder` facade: `configure → run → wait_recorded → read`, with
  `run`/`read` split so the application controls trigger timing.
- Multi-channel readback deinterleaved in one DMA pass, returned as
  `{channel: ndarray}` (or a bare array for a single channel).
- On-scope frame averaging (`average=k`, float32, NEON accumulators) and
  per-channel cropping (`crop=(lo, hi)`).
- Wire encodings that trade precision for speed: 16-bit raw (default), 12-bit
  packed (`transport="packed"`), and 8-bit (`sample_bits=8`).
- `to_volts` scaling from the cached preamble, correct across every encoding.
- Fail-closed firmware whitelist (host + agent), shipping the MHO98 / `00.01.00`
  profile.
- `ScpiControl` and `Readback` layers for direct use beneath the facade.
- Offline test suite and an on-scope validation harness
  (`python -m tools.validate_scope`).
- Self-contained wheel: the Frida agent bundle is committed and ships as package
  data, so installing needs no Node toolchain.

[Unreleased]: https://github.com/uberjay/rigol-fastrec/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/uberjay/rigol-fastrec/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/uberjay/rigol-fastrec/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/uberjay/rigol-fastrec/releases/tag/v0.1.0
