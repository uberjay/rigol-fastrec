# Reproducible toolchain for regenerating blob.generated.ts from accum.S.
#
# Uses the GNU aarch64 cross-assembler (GAS — matches accum.S's syntax exactly)
# plus its objcopy/nm. The assembler targets aarch64 regardless of the build
# host's architecture. Invoked via regen_blob.sh.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc-aarch64-linux-gnu python3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /work
ENTRYPOINT ["python3", "gen_blob.py"]
