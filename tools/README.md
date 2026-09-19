# Scope validation and firmware diagnostics

Run these commands **from the repository root**, with the package installed
(`python -m pip install -e '.[dev]'`) or `PYTHONPATH=python` set. Tools live in the
checkout, outside the installed library. Use `python -m ...` so their shared
imports resolve normally; each command supports `--help` without opening a scope.

## Routine revalidation

Connect **AFG1 → CH1** and **AFG2 → CH3**. CH2 is the undriven channel used to
check lane mapping and cross-talk. The tools configure the scope and the AFGs;
run one at a time. Each AFG used by a run is switched off on exit, including
exceptions and interrupts.

```bash
python -m tools.validate_scope --host 10.0.10.213 --csv \
    --output-dir /tmp/rigol-scope-baseline
python -m tools.validate_timestamps --host 10.0.10.213 \
    --output-dir /tmp/rigol-timestamp-baseline
```

Both output directories must be new. `--csv` also needs ADB access to the scope;
omit it to run all other scope checks. Timestamp validation uses AFG1, keeps AFG2
off, and exercises records from 1,000 through 1,000,000 samples per frame.

| Command | Coverage | Evidence |
|---|---|---|
| [`tools.validate_scope`](validate_scope.py) | Bulk samples, averaging across DMA boundaries, crops, encodings, channel mapping, streaming, metadata/scaling/NPZ, optional Rigol CSV comparison | JSON checks/configuration, NPZ captures, CSVs and comparison reports with `--csv` |
| [`tools.validate_timestamps`](validate_timestamps.py) | Public timestamp API against full-register references, chunk boundaries, subsets/crops/encodings, deliberately gapped triggers, NPZ, averaging exclusion and unchanged default averaging | Every reference tag, per-read timing/telemetry, checks, timestamped NPZ captures |

Commands return zero when their checks pass, and nonzero on failures. The scope
validator also reports skips; inspect those when comparing coverage. Always pass
`--output-dir` when collecting a baseline (required by the timestamp tools).

## Focused diagnostics

These retain the independent measurements and native probes used to develop the
API. Use them to locate a regression or to investigate a changed firmware path.
They are not extra stages required on every validation run.

| Module under `tools.diagnostics` | Question answered | Output / interpretation |
|---|---|---|
| [`frame_timestamps`](diagnostics/frame_timestamps.py) | Does recorded timing match known AFG periods and interruptions? Does the native timestamp cache agree with the SCPI/display string? | JSON with integer times, asynchronous refresh observations, repeat selections and raw captures; all checks should pass |
| [`register_timestamps`](diagnostics/register_timestamps.py) | Do per-frame register tags agree with the native SCPI/display cache, independently of bulk prefix decoding? | Full-register vectors, transformed reads, recovery checks and raw NPZ captures; all checks should pass |
| [`replay_headers`](diagnostics/replay_headers.py) | How do header tags, replay pacing and native calls interact? | Header words, frame indices, register references, DMA results, samples and optional native traces |
| [`stream_batches`](diagnostics/stream_batches.py) | How do arrival bursts and capture throughput change with trigger rate and batch size? | Per-case frame counts, arrival gaps, burst sizes, duplicates and errors; JSON with `--output-dir` |

```bash
python -m tools.diagnostics.frame_timestamps --host 10.0.10.213 \
    --output-dir /tmp/rigol-frame-times
python -m tools.diagnostics.register_timestamps --host 10.0.10.213 \
    --output-dir /tmp/rigol-register-times
python -m tools.diagnostics.replay_headers --host 10.0.10.213 \
    --study prefix --repeats 8 --output-dir /tmp/rigol-prefix-headers
python -m tools.diagnostics.stream_batches --host 10.0.10.213 \
    --rates 50,200,1000,10000 --batches 1,16,0 \
    --output-dir /tmp/rigol-stream-batches
```

`replay_headers` defaults to **prefix**, the working short-prefix path. Its
`--study pacing` and `--study trace` modes exercise full-depth transfers and can
reproduce the header/frame association mismatch documented in
[RAW_TIMESTAMPS.md](../docs/RAW_TIMESTAMPS.md). They exit nonzero when they observe
a mismatch and retain the evidence. That result is useful for diagnosis; use the
public timestamp validator to assess the API.

`stream_batches` reports rates and burst sizes as measurements. It fails on AFG,
stream, SCPI or output-cleanup errors, but does not require every trigger to become
a delivered frame at every rate. Its arrival times are host receipt times. Use
frame timestamps to measure time between recorded acquisitions.

## Revalidating after a firmware update

1. Save the routine scope and timestamp reports on the current firmware first.
2. Check the new binary's native symbols, signatures and struct offsets against
   [`agent/src/firmware.ts`](../agent/src/firmware.ts). Update the verified profile
   and the host's [`SUPPORTED_FIRMWARE`](../python/rigol_fastrec/firmware.py) entry,
   rebuild the agent, and run the offline checks. An unknown firmware is rejected
   by both host and agent; these tools do not bypass that check.
3. Run `tools.validate_scope`, then `tools.validate_timestamps`, into separate new
   directories. Compare failures/skips, exact sample/tag comparisons, timing,
   DMA chunk sizes, retries and fallback counts with the baseline.
4. If timing differs, use `frame_timestamps` and `register_timestamps` to establish
   the reference first, then `replay_headers` to inspect bulk association. The
   display observer has its own firmware check; verify its field offsets and
   symbols in [`frame_timestamps.js`](diagnostics/frame_timestamps.js) before
   updating that check. Review [`replay_headers.js`](diagnostics/replay_headers.js)
   for the replay/tracing signatures. Use `stream_batches` for streaming changes.

Reports include instrument identity, requested configuration, package/Python
versions, Git revision, the **loaded agent bundle's SHA-256**, and SHA-256 hashes
of all tool Python/JavaScript sources. Source hashes capture uncommitted edits as
well as committed versions. Preserve the full output directory, including NPZ
and optional CSV files; the report alone does not contain every waveform.

## Organization and offline checks

```text
tools/
  validate_scope.py          routine scope/metadata/CSV checks
  validate_timestamps.py     routine timestamp API checks
  diagnostics/               focused firmware and streaming investigations
    *.js                     Frida payloads beside their Python driver
  _support/
    bench.py                 AFG control, reporting, SCPI errors, source identity
    captures.py              metadata/CSV checks used by validate_scope
    timestamps.py            full-register reference shared by timestamp tools
```

```bash
python -m pytest -q
make check-agent
```

Offline tests cover helper logic, timestamp decoding, report failure handling,
AFG cleanup, source metadata, all CLI imports/help, and the adjacent Frida assets.
See [VALIDATION.md](../docs/VALIDATION.md) for measured hardware results and more
scope-validator options.
