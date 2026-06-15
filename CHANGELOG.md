# Changelog

Notable changes to rigol-fastrec. Format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/uberjay/rigol-fastrec/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/uberjay/rigol-fastrec/releases/tag/v0.1.0
