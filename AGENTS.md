# Working on rigol-fastrec

This repository contains a Python control/readback library and a Frida agent
for Rigol MHO900 scopes. Start with [README.md](README.md), then consult
[docs/API.md](docs/API.md) and [docs/DESIGN.md](docs/DESIGN.md) for the affected
path. Keep library changes separate from application-specific DUT capture code.

## Repository map

- `python/rigol_fastrec/`: the installed package. `scpi.py` owns scope control,
  `readback.py` owns the Frida/data connection, and `recorder.py` coordinates
  them through `WaveRecorder`.
- `agent/src/`: TypeScript agent; native readback, CModule code, timestamps,
  and channel layout live under `native/`. Firmware profiles are in
  `agent/src/firmware.ts`; the host also checks identity in
  `python/rigol_fastrec/firmware.py`.
- `python/tests/` and `agent/test/`: offline regression tests.
- `tools/`: live validation and diagnostics; read [tools/README.md](tools/README.md)
  and [docs/VALIDATION.md](docs/VALIDATION.md) before using a hardware harness.
- `examples/`: runnable callers of the installed library.
- `docs/`: API details and reverse-engineering evidence. Preserve the reasoning
  behind firmware workarounds, not just their resulting code.

## Development and checks

Use Python 3.12 or newer in the project's active environment. For a fresh
environment, install the checkout with its test dependencies:

```sh
python -m pip install -e '.[dev]'
python -m pytest -q
```

Ensure tests import this checkout rather than an older installed wheel. When
using a non-editable environment, `PYTHONPATH=python python -m pytest -q` runs
the checkout from the repository root.

For agent changes, use the committed npm lockfile (`npm ci` in `agent/` when
dependencies need installing), then run:

```sh
cd agent
npm run check
npm test
npm run build
```

`npm run build` regenerates `python/rigol_fastrec/_agent.js`. Commit that bundle
with agent source changes: wheels ship it so users do not need Node. Do not
hand-edit it. NEON assembly changes also require regenerating
`agent/src/native/accum/blob.generated.ts` through `make neon-blob` (Docker),
then rebuilding the agent.

Use focused regression tests while iterating and the relevant offline suite
before committing. `make check` runs both suites and installs the Python
checkout in editable mode. Hardware tests are separate; passing mocks does
not establish firmware timing or DMA correctness.

## Scope behavior that must be preserved

- Finite captures follow `configure → run → DUT triggers → wait_recorded → read`.
  Keep the trigger source quiet until `run()` returns.
- Read [docs/WAVERECORD_READINESS.md](docs/WAVERECORD_READINESS.md) before
  changing arming. `WREPlay:FCURrent? == 1` can be stale; readiness also requires
  `WREPlay:FMAX? == 1`. Do not require observing a transient zero. Distinguish
  playback FMAX from `WRECord:FMAX?`, which reports capacity.
- Preserve the widened VISA timeout during arming and restoration afterward.
  Readiness and record completion have different deadlines. Once a finite DUT
  burst has acknowledged completion, waiting longer cannot supply lost triggers.
- Frame order, channel stride, crop bounds and averaging groups determine which
  samples belong to each DUT input. Do not hide missing frames by padding or
  accepting incomplete readback. Keep DMA bounds checks and geometry-specific
  limits intact.
- Preserve firmware identity/symbol checks and explicit errors for unsupported
  hardware. Offsets and native function signatures need evidence for the target
  build; the same `*IDN?` firmware string can cover different binaries.
- Keep export/replay cleanup and socket/thread cleanup on failure paths. Do not
  return partially filled arrays as successful captures.

## Hardware and reverse engineering

Follow the user's current instructions and existing authorization for live
scope work. Before changing acquisition state, check for an active capture.
Use bounded trials and separate output paths so tests do not overwrite datasets
or interfere with another capture. Run the required cleanup even on failure.
Treat firmware installation, rebooting, and changes to persistent scope startup
as separate actions from a capture test.

For Ghidra work, explicitly select the intended program when multiple builds
are open. Record the binary/build, image base, relevant function addresses and
observed behavior in the appropriate document. Separate what the code proves
from timing hypotheses and live observations. For live results, record scope
identity, acquisition geometry, trigger settings, batch sizes, commands and
pass/failure counts; do not commit bulky captures or firmware images.

## Versions and commits

The package version is single-sourced in
`python/rigol_fastrec/__init__.py`; `pyproject.toml` reads it dynamically. The
private npm package's `0.0.0` is not the Python release version. When a version
bump is requested, update `CHANGELOG.md` with a dated release section and its
comparison links. Keep commit, tag, push and publication actions within the
requested scope, and preserve unrelated work in the checkout.
