# Automated Rigol Record CSV export

`WaveRecorder.export_csv()` invokes **Rigol's own WaveRecord CSV writer** and
pulls the resulting file over ADB for comparison with the fast DMA readback.

## Use it

Install Android platform-tools (`adb` on PATH) and enable the scope's existing
ADB connection. The default ports are 55555 for ADB, 5555 for SCPI, and 27042
for Frida.

```python
from rigol_fastrec.csv_export import compare_record_csv

# Inside an open WaveRecorder context, after configure():
rec.run(8, capture_metadata=True)
rec.wait_recorded(timeout=15)
cap = rec.read_capture(count=8)
cap.save("capture.npz")
export = rec.export_csv("reference.csv")
comparison = compare_record_csv(cap, export.waveform)
print(export.sha256, comparison)
```

The instrument must be exclusively controlled throughout capture and export.
Do not start a stream, change settings, touch the Save UI, or make simultaneous
readback calls. `export_csv` exports the **complete** record with all enabled
channels. Comparison requires a full, unaveraged, uncropped 16-bit raw capture.

Arguments:

- `adb="adb"`: executable name or path.
- `adb_serial=None`: defaults to the scope host plus `:55555`; an explicit USB
  serial is also accepted. TCP devices are connected automatically.
- `timeout=90`: export completion budget in seconds (1–295). Individual SCPI/ADB
  calls have their own bounded I/O timeouts, so total elapsed time can be longer.
- `max_values=1_000_000`: refuse larger exports before touching the instrument;
  values = recorded frames × depth × channels. CSV is a slow diagnostic path.

Local destinations must not exist. The helper creates a unique short scope
filename, checks both exact and auto-numbered names are absent, verifies native
Record-writer execution/completion, waits for save status and a stable nonempty
file, pulls it, checks its byte count and parses every row. It verifies settings
and frame count again, computes SHA-256, and removes **only its own** remote file
after successful retrieval and parsing. A transferred but malformed CSV is
retained locally for diagnosis. Failed exports invalidate the facade's record
association; record again before retrying. A timeout may leave a file/save in
progress on the scope: wait for `:SAVE:STAT?` to return `1` before retrying.

The scope's storage source/path/type settings are left changed. The helper does
not restore a previous Save-dialog configuration or change overwrite preference.

## Export implementation

The [MHO900 Programming Guide](https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/oscilloscopes/MHO900-ProgrammingGuide.pdf),
sections 3.21.9 and 3.21.11, defines screen and memory saves with the destination
path as a command argument:

```text
:SAVE:WAVeform C:/screen.csv
:SAVE:MEMory:WAVeform C:/memory.csv
```

The SCPI handlers select Screen (0) and Memory (1), respectively. To export
WaveRecord data, the agent changes that source selection to Record (2):

1. Resolve the storage symbols through the agent's firmware profile.
2. Attach a one-shot hook to `CApiStorage::ApiStorage_SetWaveDepth(int)`.
   When the Memory selection arrives, replace its argument with Record (2)
   and detach the source hook.
3. Attach an observer to `CWaveFile::saveRecordAsCSV(RString&)` to track the
   Record CSV writer's entry and return.
4. Issue `:SAVE:MEMory:WAVeform` with the unique destination path.
5. Wait for `:SAVE:STAT?` completion and a stable file, then retrieve it over ADB.

The scope maps `C:/` to `/data/UserData/`. With overwrite/overlap disabled,
`name.csv` becomes `name0.csv`; the helper checks both names. All hooks are
removed on completion, timeout, failure or agent disposal.

## What is compared

The Record CSV has a `CHnV,...,t0=...,tInc=...` header and concatenated
frames in acquisition order. The parser requires the precise requested row count,
channel columns, finite samples and valid timing fields. It rejects screen CSV,
truncation, extra rows, duplicate channels and unexpected columns.

`compare_record_csv()` checks all values with **no alignment, gain fitting,
frame reordering or tail trimming**. Its default tolerance is half one uint16
WORD-code step at the saved preamble's `y_increment`. SCPI preamble and CSV
decimal rounding cause small voltage differences; the maximum error is reported
in volts and WORD-code steps. Set `tolerance_lsb` to adjust the comparison
threshold.

The CSV's `tInc` is checked against the acquisition sample interval, and `t0`
against the saved preamble. See [Time axes](CAPTURES.md#time-axes) for the sample
time calculations.

## Automated bench coverage

```bash
python tools/validate_scope.py --host 10.0.10.213 --csv \
    --output-dir /tmp/scope-csv-validation
```

`--csv` adds exports to six metadata cases: baseline, 50 ohms, 10x probe setting,
gapped `{1,3,4}` lanes, NORM edge triggers, and inversion with deskew. Each case
compares every frame/channel, re-reads the raw record afterward, and requires
byte-for-byte unchanged samples. Reports include native writer status, local
filename, hash and per-channel errors. CSV/NPZ files and per-export JSON stay in
`--output-dir`. Without that option they are temporary. With `--csv`, an export
or comparison error is a FAIL. Without it, the CSV check is SKIP. See
[VALIDATION.md](VALIDATION.md) for bench results.
