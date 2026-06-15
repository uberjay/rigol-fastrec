#!/usr/bin/env bash
# Regenerate blob.generated.ts from accum.S in a reproducible toolchain
# container — no local aarch64 toolchain needed, just Docker. Run from anywhere:
#
#   agent/src/native/accum/regen_blob.sh
#
# Builds the GNU aarch64 cross-assembler image (Dockerfile here), mounts this
# directory in, and runs gen_blob.py, which writes blob.generated.ts back out.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
img="rigol-fastrec-neon-blob"
docker build -q -t "$img" "$here"
docker run --rm -v "$here:/work" "$img"
