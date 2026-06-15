#!/usr/bin/env python3
"""Assemble accum.S (aarch64 NEON) and (re)generate blob.generated.ts.

Runs INSIDE the toolchain container (see Dockerfile / regen_blob.sh) so the
output is reproducible on any host. Don't run this directly on the host;
use ../accum/regen_blob.sh.

Steps: assemble accum.S → extract the raw .text bytes → read each function's
byte offset from the symbol table → emit the TS module (NEON_ACCUM_BYTES +
NEON_ACCUM_OFF) the CModule loader imports.
"""

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "accum.S"
OUT = HERE / "blob.generated.ts"
TOOL = "aarch64-linux-gnu-"          # GNU cross toolchain prefix (see Dockerfile)
# Export keys, in emit order. The asm labels carry a leading underscore; nm
# names are matched with it stripped.
FUNCS = ["accum_s1", "accum_s2"]

OBJ = "/tmp/accum.o"
BIN = "/tmp/accum.text.bin"
RET = [0xc0, 0x03, 0x5f, 0xd6]       # `ret` (0xd65f03c0) in little-endian bytes


def run(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def main() -> None:
    # 1. Assemble for aarch64 (ELF). ASIMD/NEON is ARMv8-A baseline, so ld1/ld2/
    #    uaddw need no -march flag.
    run(f"{TOOL}gcc", "-c", str(SRC), "-o", OBJ)

    # 2. Exact .text bytes — raw binary of just that section.
    run(f"{TOOL}objcopy", "-O", "binary", "--only-section=.text", OBJ, BIN)
    blob = list(Path(BIN).read_bytes())
    if not blob:
        sys.exit("empty .text — assembly produced no code")

    # 3. Per-function byte offset within .text (single-section object → nm values
    #    are section-relative). Strip any leading underscore to get the C name.
    off: dict[str, int] = {}
    for line in run(f"{TOOL}nm", "--numeric-sort", OBJ).splitlines():
        m = re.match(r"([0-9a-fA-F]+)\s+[tT]\s+(\S+)", line)
        if m and m.group(2).lstrip("_") in FUNCS:
            off[m.group(2).lstrip("_")] = int(m.group(1), 16)
    missing = [f for f in FUNCS if f not in off]
    if missing:
        sys.exit(f"symbols not found in {OBJ}: {missing}")

    # 4. Sanity: each function 4-aligned; blob ends in RET (functions are leaf,
    #    the last instruction emitted is accum_s2's ret).
    for f, o in off.items():
        if o % 4:
            sys.exit(f"{f} offset {o} not 4-aligned — extraction bug?")
    if blob[-4:] != RET:
        sys.exit(f"blob doesn't end in RET; got {blob[-4:]} — extraction bug?")

    # 5. Emit the TS module.
    rows = "\n".join(
        "    " + ", ".join(f"0x{b:02x}" for b in blob[i:i + 16]) + ","
        for i in range(0, len(blob), 16))
    offs = "\n".join(f"    {f}: {off[f]}," for f in FUNCS)
    OUT.write_text(f"""\
// GENERATED from accum.S by gen_blob.py — DO NOT EDIT BY HAND.
//
// Assembled machine code for the aarch64 NEON uint16→uint32 accumulators in
// accum.S. The agent assembles nothing at runtime: these bytes are patched into
// RWX memory and called from the CModule. Regenerate with regen_blob.sh (Docker;
// see agent/README.md).
//
// {len(blob)} bytes. Offsets are byte positions of each function within the blob.
// Coverage: accum_s1 (stride-1, contiguous) and accum_s2 (stride-2 LD2, the
// 2-channel-interleave case). Stride 4 (3–4 enabled channels) uses the scalar
// fallback in fastrec_send_averaged_frames.

export const NEON_ACCUM_BYTES: number[] = [
{rows}
];

/** Byte offset of each accumulator within NEON_ACCUM_BYTES. */
export const NEON_ACCUM_OFF: {{ readonly [name: string]: number }} = {{
{offs}
}};
""")
    print(f"wrote {OUT} ({len(blob)} bytes; offsets {off})")


if __name__ == "__main__":
    main()
