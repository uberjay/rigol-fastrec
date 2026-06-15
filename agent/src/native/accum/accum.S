// NEON uint16→uint32 accumulators for the in-agent averaging readback.
//
// These replace the TinyCC -O0 scalar accumulate in the hot path of
// fastrec_send_averaged_frames. The agent assembles NOTHING at runtime — the
// bytes of these functions are extracted on an aarch64-capable host (ARMv8 NEON
// encodings are identical to the scope's aarch64) into blob.generated.ts as a
// constant, patched into RWX memory, and called from the CModule like the
// raw_syscall stub. Regenerate with regen_blob.sh (Docker; see agent/README.md).
//
// Each function ADDS one frame's (deinterleaved, cropped) samples into a uint32
// accumulator; the caller zeroes the accumulator at the start of each k-group.
// Widening uint16→uint32 is UADDW/UADDW2 (low/high halves into 2×4 uint32).
// Leaf functions: no stack frame, only caller-saved regs (v0-v3), just ret.

        .text

// void accum_s1(uint32_t *acc, const uint16_t *src, uint32_t n)
//   acc[j] += src[j]            (single channel — contiguous samples)
        .globl _accum_s1
        .p2align 4
_accum_s1:
1:      cmp     w2, #8
        b.lo    2f
        ld1     {v0.8h}, [x1], #16
        ldp     q2, q3, [x0]
        uaddw   v2.4s, v2.4s, v0.4h
        uaddw2  v3.4s, v3.4s, v0.8h
        stp     q2, q3, [x0], #32
        sub     w2, w2, #8
        b       1b
2:      cbz     w2, 4f
3:      ldrh    w4, [x1], #2
        ldr     w5, [x0]
        add     w5, w5, w4
        str     w5, [x0], #4
        subs    w2, w2, #1
        b.ne    3b
4:      ret

// void accum_s2(uint32_t *acc, const uint16_t *src, uint32_t n, uint32_t off)
//   acc[j] += src[off + 2*j]    (2-channel interleave; off = 0 or 1)
// LD2 deinterleaves into v0 (even/off 0) and v1 (odd/off 1).
        .globl _accum_s2
        .p2align 4
_accum_s2:
        cbnz    w3, 20f
        cmp     w2, #8
        b.lo    12f
11:     ld2     {v0.8h, v1.8h}, [x1], #32
        ldp     q2, q3, [x0]
        uaddw   v2.4s, v2.4s, v0.4h
        uaddw2  v3.4s, v3.4s, v0.8h
        stp     q2, q3, [x0], #32
        sub     w2, w2, #8
        cmp     w2, #8
        b.hs    11b
12:     cbz     w2, 30f
13:     ldrh    w4, [x1], #4
        ldr     w5, [x0]
        add     w5, w5, w4
        str     w5, [x0], #4
        subs    w2, w2, #1
        b.ne    13b
        ret
20:     cmp     w2, #8
        b.lo    22f
21:     ld2     {v0.8h, v1.8h}, [x1], #32
        ldp     q2, q3, [x0]
        uaddw   v2.4s, v2.4s, v1.4h
        uaddw2  v3.4s, v3.4s, v1.8h
        stp     q2, q3, [x0], #32
        sub     w2, w2, #8
        cmp     w2, #8
        b.hs    21b
22:     cbz     w2, 30f
        add     x1, x1, #2
23:     ldrh    w4, [x1], #4
        ldr     w5, [x0]
        add     w5, w5, w4
        str     w5, [x0], #4
        subs    w2, w2, #1
        b.ne    23b
30:     ret
