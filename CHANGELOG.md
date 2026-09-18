# Changelog

Notable changes to rigol-fastrec. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

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
- `tools/stream_batch_probe.py`: measures the stream loop's batching and
  delivered fraction against an AFG-paced trigger across rates.
- `examples/stream_viewer.py`: live pyqtgraph viewer over `stream()`, installed
  with the new `viewer` extra (`pip install -e '.[viewer]'`).
- Streaming checks in `tools/validate_scope.py` (shape, live re-capture, each
  encoding, crop, multichannel, AFG frequency, and `read()` after a stream);
  `--no-stream` skips them.

### Changed
- `tools/validate_scope.py` defaults now assume AFG1→CHAN1 and AFG2→CHAN3 with
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
  (`tools/validate_scope.py`).
- Self-contained wheel: the Frida agent bundle is committed and ships as package
  data, so installing needs no Node toolchain.

[Unreleased]: https://github.com/uberjay/rigol-fastrec/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/uberjay/rigol-fastrec/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/uberjay/rigol-fastrec/releases/tag/v0.1.0
