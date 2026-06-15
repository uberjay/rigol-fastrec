# rigol-fastrec design notes

How it works, and the non-obvious decisions behind it. For usage see the
[README](../README.md) and [API.md](API.md).

## Architecture

Two independent host↔scope channels, three layers:

```
  host (Python)
    SCPI / pyvisa   ── ScpiControl ─┐
    (:5555)                         ├─►  WaveRecorder (facade) ──► your app
    Frida + TCP data ── Readback ───┘     configure → run →
    (:27042 + :5028)                      wait_recorded → read
```

- `ScpiControl` (pyvisa) owns the control side: the `*IDN?` firmware check, the
  vertical/trigger/timebase/MDEP `configure()`, and the WaveRecord lifecycle
  (run → ready → recorded → replay).
- `Readback` (Frida) injects the agent and streams recorded frames back over a
  dedicated TCP data socket.
- `WaveRecorder` drives the two. The app fires its DUT triggers between `run()`
  and `wait_recorded()`.

## How readback works

The agent is TypeScript compiled with `frida-compile` into the committed
`rigol_fastrec/_agent.js`, injected into the `RIGOL.SCOPE` process.

- One `SetRun(count=N)` + one `DevAnalyzeTrace_Read` DMAs a whole frame range per
  chunk (≤ `dwMaxFrameCount`), contiguous, no per-frame header.
- Deinterleave, crop, average, encode, and the blocking socket `write()` all run
  in C (a Frida `CModule`), so the NIC drains during the next chunk's DMA: the
  read and the send overlap. NEON accumulators (`accum.S`) do the exact uint32
  frame-averaging.
- Wire: per (output row, channel), one record `<u32 nSamp><payload>` over the
  data socket. Payload is `u16`, 12-bit packed (2→3 bytes), `u8`, or float32
  averages. The `u32` prefix is the sample count, not the byte count.
- The engine→agent DMA runs at ~470 MB/s, so the readback link is always the
  bottleneck. The win is in shipping fewer or denser bytes (crop, average,
  `sample_bits`/`transport`).

## Fail-closed firmware safety

Every native offset (`dwMaxFrameCount`, the SPU-setup symbols, the `SetRun` arg
layout) is firmware-specific. Applied to an unprofiled build, they could wedge
the scope. So the identity check fails closed, keyed on `*IDN?` (model and
firmware version):

1. The host checks `*IDN?` against `firmware.ts`'s whitelist before attaching
   Frida. Unknown means abort, nothing touched.
2. The agent re-checks `(model, fwVersion)` against the same whitelist as the
   first thing `resolve()` does. A mismatch, or any required symbol failing to
   resolve, throws `UnsupportedFirmware`, and nothing hooks or runs.

No `.so` hashing: an FNV/SHA over the ~60 MB `libscope-auklet.so` was far too
slow on-scope, and the embedded version symbol read back empty, so the `*IDN?`
pair is the identity key. The binary still gets checked implicitly, since every
symbol the readback needs must resolve or `resolve()` refuses. `firmware.ts`
holds the whitelist as data (`{model, fwVersion, imageBase, symbols, offsets}`);
to support a new build, add a validated profile entry. There's one today:
`MHO98 / 00.01.00`.

## Multi-channel interleave

The FPGA runs in 1-, 2-, or 4-channel mode (the interleave stride) and packs the
enabled channels into one stream. The agent reads the live layout and
deinterleaves each requested channel by its lane in a single DMA pass.

- 4-channel mode (3 or 4 enabled): all four physical lanes stream, so a channel
  sits at lane `n-1` and gaps for disabled channels stay (with {1,2,4} enabled,
  CH4 is at lane 3, not a packed lane 2).
- 1- or 2-channel mode: the enabled channels pack into the available lanes.
- The trigger source channel is enabled implicitly, so it counts toward the
  stride and its edge is visible. MDEP caps lower as more channels enable, so
  `configure()` snaps depth against the post-enable cap.
