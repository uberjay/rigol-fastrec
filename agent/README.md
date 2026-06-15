# rigol-fastrec agent

The Frida agent injected into the scope UI application (`RIGOL.SCOPE`), built
with [frida-compile](https://github.com/frida/frida-compile) from TypeScript.

```bash
npm install          # first run; pin versions here (match deployed frida 17.x)
npm run check        # tsc --noEmit  (the offline check; replaces node --check)
npm run build        # frida-compile src/index.ts -> ../python/rigol_fastrec/_agent.js
```

`npm run build` writes the bundle straight to `../python/rigol_fastrec/_agent.js`,
which is committed so the Python package ships self-contained (no Node to run).
Rebuild + commit it whenever you change any of the agent code.

## Safety

The agent **fails closed**: `resolve()` fingerprints
`libscope-auklet.so` against `firmware.ts`'s whitelist and throws
`UnsupportedFirmware` on anything it doesn't recognize — it never applies
hardcoded offsets to an unknown binary.

## The SIMD blob

`src/native/accum/blob.generated.ts` holds the assembled machine-code bytes of
the SIMD uint16→uint32 accumulators in `accum.S`. The agent never assembles at
runtime — it patches these bytes into RWX memory and calls them from the CModule.

The bytes are checked in. To regenerate after editing `accum.S`, run (Docker is
the only prerequisite — it builds a pinned GNU aarch64 cross-assembler, so the
output is reproducible on any host):

```bash
src/native/accum/regen_blob.sh
```

It assembles `accum.S`, extracts the `.text` bytes + per-function offsets, and
rewrites `blob.generated.ts`. `Dockerfile` + `gen_blob.py` next to it are the
toolchain image and the extractor that runs inside it.
