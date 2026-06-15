// Readback core: the CModule hot loop + the machine-code stubs it calls.
//
// The per-frame strip/crop/average + blocking write() runs in C (TinyCC via
// Frida's CModule) because a JS writeAll is async — bytes only flush when the
// GLib loop runs, and a blocking DMA read starves it, so readout and transmit
// can't overlap. The C loop's synchronous write() copies into the kernel send
// buffer and returns, so the NIC drains during the next chunk's DMA.
//
// Two machine-code stubs are supplied to the CModule as symbols (TinyCC has no
// inline asm): raw_syscall (svc directly, bypassing bionic's errno path which
// faults from a Frida thread) and the NEON uint16→uint32 accumulators
// (accum.S, baked into blob.generated.ts). Mutable state lives in a JS-owned
// rw buffer (g_state), NOT CModule globals — Frida maps the CModule R-X.
//
// NOTE: behavior is only provable on-scope.

import { NEON_ACCUM_BYTES, NEON_ACCUM_OFF } from "./accum/blob.generated.js";

// g_state long[8] indices (must match the C #defines below).
export const ST_CLIENTFD = 0;
export const ST_ERR = 1;
export const ST_FRAMES = 2;
export const ST_BYTES = 3;
export const ST_ACCEPT = 4;
export const ST_PHASE = 5;

/** Resolve a libc symbol via the libc module instance (reliable on Frida 17),
 *  falling back to the global export search. */
let libcModule: Module | null = null;
export function findLibc(name: string): NativePointer {
    if (libcModule === null) {
        libcModule = Process.findModuleByName("libc.so");
        if (libcModule === null) {
            const c = Process.enumerateModules().filter(
                (m) => m.name === "libc.so" || /\/libc\.so$/.test(m.name));
            if (c.length) libcModule = c[0];
        }
        if (libcModule === null) throw new Error("libc.so module not found");
    }
    const a = libcModule.findExportByName(name)
        ?? Module.findExportByName(null, name);
    if (a === null || a.isNull()) throw new Error("libc symbol not found: " + name);
    return a;
}

// Hand-encoded arm64 syscall stub: raw_syscall(n, a0..a3) → x8=n, x0..x3=args,
// svc #0, ret. Returns the kernel result / -errno in x0.
let rawSyscallCode: NativePointer | null = null;
export function buildRawSyscall(): NativePointer {
    if (rawSyscallCode !== null) return rawSyscallCode;
    const code = Memory.alloc(Process.pageSize);
    Memory.patchCode(code, 28, (p) => {
        p.writeByteArray([
            0xe8, 0x03, 0x00, 0xaa,   // mov x8, x0   (syscall number)
            0xe0, 0x03, 0x01, 0xaa,   // mov x0, x1   (arg0)
            0xe1, 0x03, 0x02, 0xaa,   // mov x1, x2   (arg1)
            0xe2, 0x03, 0x03, 0xaa,   // mov x2, x3   (arg2)
            0xe3, 0x03, 0x04, 0xaa,   // mov x3, x4   (arg3)
            0x01, 0x00, 0x00, 0xd4,   // svc #0
            0xc0, 0x03, 0x5f, 0xd6,   // ret
        ]);
    });
    rawSyscallCode = code;
    return code;
}

// Patch the baked NEON accumulators (accum.S → blob.generated.ts) into RWX and
// hand back per-function entry pointers. Same mechanism as buildRawSyscall.
interface NeonBlob { base: NativePointer; accum_s1: NativePointer; accum_s2: NativePointer; }
let neonBlobCode: NeonBlob | null = null;
export function buildNeonBlob(): NeonBlob {
    if (neonBlobCode !== null) return neonBlobCode;
    const code = Memory.alloc(Process.pageSize);
    Memory.patchCode(code, NEON_ACCUM_BYTES.length, (p) => {
        p.writeByteArray(NEON_ACCUM_BYTES);
    });
    neonBlobCode = {
        base: code,
        accum_s1: code.add(NEON_ACCUM_OFF.accum_s1),
        accum_s2: code.add(NEON_ACCUM_OFF.accum_s2),
    };
    return neonBlobCode;
}

// The CModule C source. Validated; do not edit the algorithm without an
// on-scope byte-identical re-check. (No backticks — they'd close this template
// literal early; the classic agent trap.)
const CMODULE_SRC = `
#include <stdint.h>
#include <string.h>

extern int  setsockopt(int fd, int level, int opt, const void *val, uint32_t len);
extern long raw_syscall(long n, long a0, long a1, long a2, long a3);
extern void accum_s1(uint32_t *acc, const uint16_t *src, uint32_t n);
extern void accum_s2(uint32_t *acc, const uint16_t *src, uint32_t n, uint32_t off);
#define SYS_accept4 242
#define SYS_write   64
#define SYS_fcntl   25
#define F_SETFL     4
#define O_NONBLOCK  0x800

/* Mutable state in a JS-allocated rw buffer (CModule globals are R-X). long[8]. */
extern long g_state[];
#define ST_CLIENTFD 0
#define ST_ERR      1
#define ST_FRAMES   2
#define ST_BYTES    3
#define ST_ACCEPT   4
#define ST_PHASE    5

int fastrec_ping(int x) { return x * 2 + 1; }

/* Diagnostic: getpid via the raw_syscall stub (172 = __NR_getpid on arm64).
 * No block, no network — isolates "is raw_syscall callable" from accept4. */
long fastrec_test_getpid(void) { return raw_syscall(172, 0, 0, 0, 0); }

/* Blocking write() of a whole buffer over the client socket (busy-retries on
 * EAGAIN). Returns 0, or -3 on a closed/errored socket. */
static int wr_all(int fd, uint8_t *p, uint32_t left) {
    while (left > 0) {
        long w = raw_syscall(SYS_write, fd, (long)p, (long)left, 0);
        if (w == -11 /* EAGAIN */) continue;
        if (w <= 0) return -3;
        p += w;
        left -= (uint32_t)w;
    }
    return 0;
}

/* Accept one client on the (JS-created, non-blocking) listen fd via raw
 * accept4, store the fd. Returns the fd, or negative -errno (EAGAIN if none). */
int fastrec_accept(int listenfd) {
    raw_syscall(SYS_fcntl, (long)listenfd, F_SETFL, O_NONBLOCK, 0);
    long c = raw_syscall(SYS_accept4, (long)listenfd, 0, 0, 0);
    g_state[ST_ACCEPT] = c;
    if (c < 0) return (int)c;
    g_state[ST_CLIENTFD] = c;
    return (int)c;
}

/* Deinterleave/crop/write N already-DMA'd frames (one SetRun(count=N) +
 * DevAnalyzeTrace_Read done in JS). Frames are contiguous, no per-frame header.
 * Each frame interleaves stride channels; offsets[0..nch) are the requested
 * channels' lanes. Per frame, one wire record per channel IN offsets ORDER; the
 * u32 prefix is always the SAMPLE count (not the byte count). outBits picks
 * the payload encoding:
 *   16: <u32 nSamp><nSamp * u16>            raw 16-bit codes
 *   12: <u32 nSamp><ceil(nSamp/2)*3 bytes>  top 12 bits (code>>4) packed 2->3
 *       bytes, LE nibble layout: b0=s0[7:0], b1=s0[11:8]|s1[3:0]<<4, b2=s1[11:4]
 *       (saves 25% on the wire; host shifts <<4 back to the 16-bit domain).
 *    8: <u32 nSamp><nSamp * u8>             top 8 bits (code>>8), half the wire.
 * g_state FRAMES/BYTES accumulate across chunk calls (JS resets before chunk 0). */
int fastrec_send_frames(uint16_t *src, uint32_t frameCount,
                        uint32_t engineSamples, uint32_t stride,
                        const uint32_t *offsets, uint32_t nch,
                        uint32_t cropLo, uint32_t cropHi, uint32_t outBits,
                        uint8_t *frameBuf, uint32_t frameBufCap) {
    int fd = (int)g_state[ST_CLIENTFD];
    if (fd < 0) { g_state[ST_ERR] = -1; return -1; }
    uint32_t full = (stride <= 1) ? engineSamples : engineSamples / stride;
    uint32_t lo = 0, hi = full;
    if (cropHi > cropLo) {
        lo = cropLo; hi = cropHi;
        if (hi > full) hi = full;
        if (lo > hi) lo = hi;
    }
    uint32_t nSamp = hi - lo;
    uint32_t payloadBytes = (outBits == 12) ? ((nSamp + 1) / 2) * 3
                          : (outBits == 8)  ? nSamp
                          :                   nSamp * 2;
    if (4 + payloadBytes > frameBufCap) { g_state[ST_ERR] = -2; return -2; }
    *(uint32_t *)frameBuf = nSamp;
    uint32_t step = (stride <= 1) ? 1 : stride;     // sample j = p0[j*step]
    for (uint32_t i = 0; i < frameCount; i++) {
        uint16_t *cap = src + (uint64_t)i * engineSamples;
        for (uint32_t c = 0; c < nch; c++) {
            uint32_t off = (stride <= 1) ? 0 : offsets[c];
            uint16_t *p0 = cap + (uint64_t)lo * step + off;
            if (outBits == 12) {
                uint8_t *o = frameBuf + 4;
                uint32_t j = 0;
                for (; j + 1 < nSamp; j += 2) {
                    uint32_t s0 = p0[(uint64_t)j * step] >> 4;
                    uint32_t s1 = p0[(uint64_t)(j + 1) * step] >> 4;
                    *o++ = (uint8_t)(s0 & 0xFF);
                    *o++ = (uint8_t)((s0 >> 8) | ((s1 & 0x0F) << 4));
                    *o++ = (uint8_t)(s1 >> 4);
                }
                if (nSamp & 1) {        /* odd tail: pad the pair's 2nd sample */
                    uint32_t s0 = p0[(uint64_t)(nSamp - 1) * step] >> 4;
                    *o++ = (uint8_t)(s0 & 0xFF);
                    *o++ = (uint8_t)(s0 >> 8);
                    *o++ = 0;
                }
            } else if (outBits == 8) {
                uint8_t *o = frameBuf + 4;
                for (uint32_t j = 0; j < nSamp; j++)
                    o[j] = (uint8_t)(p0[(uint64_t)j * step] >> 8);   /* top 8 bits */
            } else {
                uint16_t *dst = (uint16_t *)(frameBuf + 4);
                if (stride <= 1)
                    memcpy(dst, cap + lo, (uint64_t)nSamp * 2);
                else
                    for (uint32_t j = 0; j < nSamp; j++) dst[j] = p0[(uint64_t)j * step];
            }
            int e = wr_all(fd, frameBuf, 4 + payloadBytes);
            if (e) { g_state[ST_ERR] = e; return e; }
            g_state[ST_BYTES]  += 4 + payloadBytes;
            g_state[ST_FRAMES] += 1;
        }
    }
    return 0;
}

/* Averaging variant: sum k consecutive frames into EXACT uint32 accumulators
 * (a float32 one loses precision past 2^24), ship one float32 mean per group.
 * Per channel: a separate accumulator accum[c*outCount ..] and one float32
 * record per completed group, in offsets ORDER. The accumulators + partial-group
 * fill persist across chunk calls (a group can span chunks), so JS owns them
 * (and sizes accum to nch*outCount). Wire: <u32 outCount><outCount * float32>. */
int fastrec_send_averaged_frames(uint16_t *src, uint32_t frameCount, uint32_t engineSamples,
                      uint32_t stride, const uint32_t *offsets, uint32_t nch,
                      uint32_t cropLo, uint32_t cropHi, uint32_t k, uint32_t *accum,
                      uint32_t *fillPtr, uint8_t *outBuf, uint32_t outBufCap) {
    int fd = (int)g_state[ST_CLIENTFD];
    if (fd < 0) { g_state[ST_ERR] = -1; return -1; }
    if (k < 1) k = 1;
    uint32_t full = (stride <= 1) ? engineSamples : engineSamples / stride;
    uint32_t lo = 0, hi = full;
    if (cropHi > cropLo) {
        lo = cropLo; hi = cropHi;
        if (hi > full) hi = full;
        if (lo > hi) lo = hi;
    }
    uint32_t outCount = hi - lo;
    if (4 + outCount * 4 > outBufCap) { g_state[ST_ERR] = -2; return -2; }
    float *out = (float *)(outBuf + 4);
    *(uint32_t *)outBuf = outCount;
    uint32_t fill = *fillPtr;
    for (uint32_t i = 0; i < frameCount; i++) {
        uint16_t *base = src + (uint64_t)i * engineSamples + (uint64_t)lo * stride;
        if (fill == 0)
            for (uint32_t j = 0; j < nch * outCount; j++) accum[j] = 0;
        for (uint32_t c = 0; c < nch; c++) {
            uint32_t *acc = accum + (uint64_t)c * outCount;
            uint32_t off = offsets[c];
            if (stride == 1) {
                accum_s1(acc, base, outCount);
            } else if (stride == 2) {
                accum_s2(acc, base, outCount, off);
            } else {
                uint16_t *p = base + off;
                for (uint32_t j = 0; j < outCount; j++) { acc[j] += *p; p += stride; }
            }
        }
        if (++fill == k) {
            float inv = 1.0f / (float)k;
            for (uint32_t c = 0; c < nch; c++) {
                uint32_t *acc = accum + (uint64_t)c * outCount;
                for (uint32_t j = 0; j < outCount; j++) out[j] = (float)acc[j] * inv;
                int e = wr_all(fd, outBuf, 4 + outCount * 4);
                if (e) { g_state[ST_ERR] = e; return e; }
                g_state[ST_BYTES]  += 4 + outCount * 4;
                g_state[ST_FRAMES] += 1;
            }
            fill = 0;
        }
    }
    *fillPtr = fill;
    return 0;
}
`;

/** Native handles built once and reused. */
export interface ReadbackCModule {
    /** The live CModule. MUST be retained: Frida frees a CModule's code pages
     *  when its JS object is GC'd, leaving every NativeFunction below dangling
     *  (faults at the freed page base). Keeping it in the cached struct pins it. */
    cmod: CModule;
    /** fastrec_accept(listenfd) → client fd or -errno. */
    accept: NativeFunction<number, [number]>;
    /** fastrec_send_frames(src,frameCount,engSamp,stride,offsets,nch,cropLo,cropHi,outBits,frameBuf,cap). */
    sendFrames: NativeFunction<number,
        [NativePointer, number, number, number, NativePointer, number, number, number,
         number, NativePointer, number]>;
    /** fastrec_send_averaged_frames(src,frameCount,engSamp,stride,offsets,nch,cropLo,cropHi,k,accum,fill,outBuf,cap). */
    sendAveragedFrames: NativeFunction<number,
        [NativePointer, number, number, number, NativePointer, number, number, number, number,
         NativePointer, NativePointer, NativePointer, number]>;
    /** long[8] mutable state buffer (ST_* indices). */
    gState: NativePointer;
    /** Diagnostics. */
    ping: NativeFunction<number, [number]>;
    testGetpid: NativeFunction<number | Int64, []>;
}

/** Build the symbol table the CModule links against (libc + machine-code stubs
 *  + the JS-owned g_state buffer). */
function buildCModuleSymbols(): { [name: string]: NativePointer } {
    const neon = buildNeonBlob();
    const gState = Memory.alloc(64);
    gState.writeByteArray(new Array(64).fill(0));
    gState.writeS64(int64(-1));   // ST_CLIENTFD = -1
    return {
        setsockopt: findLibc("setsockopt"),
        raw_syscall: buildRawSyscall(),
        accum_s1: neon.accum_s1,
        accum_s2: neon.accum_s2,
        g_state: gState,
    };
}

let cmodule: ReadbackCModule | null = null;

/** Build the readback CModule + machine-code stubs once. Idempotent. */
export function ensureCModule(): ReadbackCModule {
    if (cmodule !== null) return cmodule;
    const syms = buildCModuleSymbols();
    const gState = syms.g_state;

    const cm = new CModule(CMODULE_SRC, syms);

    const EX = { scheduling: "exclusive" } as const;
    cmodule = {
        cmod: cm,
        accept: new NativeFunction(cm.fastrec_accept, "int", ["int"], EX),
        sendFrames: new NativeFunction(cm.fastrec_send_frames, "int",
            ["pointer", "uint32", "uint32", "uint32", "pointer", "uint32",
             "uint32", "uint32", "uint32", "pointer", "uint32"], EX),
        sendAveragedFrames: new NativeFunction(cm.fastrec_send_averaged_frames, "int",
            ["pointer", "uint32", "uint32", "uint32", "pointer", "uint32",
             "uint32", "uint32", "uint32", "pointer", "pointer", "pointer", "uint32"], EX),
        gState,
        ping: new NativeFunction(cm.fastrec_ping, "int", ["int"]),
        testGetpid: new NativeFunction(cm.fastrec_test_getpid, "long", [], EX),
    };
    return cmodule;
}

/** Size of the NEON accumulator blob (diagnostic / info()). */
export const NEON_BLOB_SIZE = NEON_ACCUM_BYTES.length;
