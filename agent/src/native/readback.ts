// Multi-frame WaveRecord readback engine.
//
// One SetRun(count=N) + one DevAnalyzeTrace_Read DMAs a whole frame RANGE
// (contiguous, no per-frame header). This amortizes the arm/DMA cost across up
// to dwMaxFrameCount frames per chunk. Orchestration is JS (reusing the
// resolved NativeFunctions); strip/crop/average + the blocking socket write of
// each chunk stay in C (cmodule.ts), preserving the kernel-send-buffer overlap.
//
// Implemented paths: raw multi-frame (average=1, 16/12/8-bit), frame-averaging
// (average=k → float32), crop, and N-channel deinterleave in one DMA pass.

import { ensureResolved, type ResolvedFns } from "./resolve.js";
import { ensureCModule, ST_ERR, ST_FRAMES, ST_BYTES } from "./cmodule.js";
import { channelLayout, chanOffsetWithinEnabled } from "./layout.js";
import { acceptOnce } from "../transport.js";

interface ArmArgs {
    wave: number[];      // 6 x u32 (DevAcquireSPU_SetWaveRange args)
    tx: number[];        // 2 x u32 (DevAcquireSpu_SetTxInfo args)
    mask: number;
    recordLen: UInt64;
    interval: UInt64;
    p9: UInt64;
}

// Per-readback scratch (grown as needed), persisted across chunk calls.
let chunkBuf: NativePointer | null = null, chunkBytes = 0;
let frameBuf: NativePointer | null = null, frameBytes = 0;
let accumBuf: NativePointer | null = null, accumBytes = 0;
let fillBuf: NativePointer | null = null;
let avgOutBuf: NativePointer | null = null, avgOutBytes = 0;
let armArgs: ArmArgs | null = null;
let armArgsKey = -1;       // engineSamples the cached args were primed at

// dwMaxFrameCount (CDrvParam+0x60): how many frames of the current MDEP fit in
// record memory, i.e. the most a single SetRun can arm. This is the bound on the
// per-SetRun chunk. It's MDEP-derived, so read it fresh each readback (it changes
// with depth).
function clampChunk(scope: NativePointer, off: Readonly<Record<string, number>>,
                    total: number): { chunk: number; hwMax: number } {
    const hwMax = scope.add(off.drvParam).add(off.dwMaxFrameCount).readU32() | 0;
    if (hwMax < 1) throw new Error(
        `dwMaxFrameCount read ${hwMax} at CDrvParam+0x${off.dwMaxFrameCount.toString(16)}; `
        + "cannot size the readback chunk");
    let chunk = total > hwMax ? hwMax : total;
    if (chunk < 1) chunk = 1;
    return { chunk, hwMax };
}

// Prime the SPU/SetRun args by hooking the firmware's own single-frame export
// for one frame and capturing what it passes. Config-derived (MDEP + layout),
// so cached and re-primed only when engineSamples changes.
function primeArmArgs(r: ResolvedFns, firstFrame: number, engineSamples: number): ArmArgs {
    if (armArgs !== null && armArgsKey === engineSamples) return armArgs;
    const cap: ArmArgs = {
        wave: [], tx: [], mask: 0,
        recordLen: uint64(0), interval: uint64(0), p9: uint64(0),
    };
    const hWave = Interceptor.attach(r.setWaveRangeAddr, { onEnter(a) {
        cap.wave = [a[0].toUInt32(), a[1].toUInt32(), a[2].toUInt32(),
                    a[3].toUInt32(), a[4].toUInt32(), a[5].toUInt32()];
    } });
    const hTx = Interceptor.attach(r.setTxInfoAddr, { onEnter(a) {
        cap.tx = [a[0].toUInt32(), a[1].toUInt32()];
    } });
    const hRun = Interceptor.attach(r.setRunAddr, { onEnter(a) {
        cap.mask = a[0].toUInt32();
        cap.recordLen = uint64(a[2].toString());
        cap.interval = uint64(a[7].toString());
        cap.p9 = uint64(a[8].toString());
    } });
    try {
        const buf = Memory.alloc(engineSamples * 2 + 64);
        const cnt = Memory.alloc(4); cnt.writeU32(engineSamples);
        r.setPlyCurr(firstFrame | 0); r.setPlyBase(firstFrame | 0);
        r.exportRecordData(0, cnt, buf);
    } finally {
        hWave.detach(); hTx.detach(); hRun.detach();
    }
    if (cap.wave.length === 0 || cap.tx.length === 0) {
        throw new Error("multi-frame: failed to capture SPU args from export");
    }
    armArgs = cap; armArgsKey = engineSamples;
    return cap;
}

// Set up the SPU + arm SetRun(count=chunkN) for frames base..base+chunkN-1.
function armChunk(r: ResolvedFns, scope: NativePointer, args: ArmArgs,
                  base: number, chunkN: number): void {
    r.setPlyCurr(base); r.setPlyBase(base);
    r.setProcChEn(1); r.setProcLaEn(0);
    r.setWaveRange(args.wave[0], args.wave[1], args.wave[2],
                   args.wave[3], args.wave[4], args.wave[5]);
    r.setTxInfo(args.tx[0], args.tx[1]);
    r.txFrmHead(1);
    r.stopScope(scope, 1);
    r.setRun(args.mask, 4, args.recordLen, chunkN, base, base,
             base + chunkN - 1, args.interval, args.p9);
    r.setIntxOut(0, 0, 0);
}

// Arm one chunk and read it into chunkBuf. On a short read (the replay engine
// lagging a fresh arm) settle briefly, re-arm, retry — segment memory is
// unchanged so re-reading the same range is idempotent.
function readChunk(r: ResolvedFns, scope: NativePointer, args: ArmArgs,
                   base: number, n: number, want: number):
        { ret: number; retries: number } {
    for (let attempt = 0; attempt < 4; attempt++) {
        armChunk(r, scope, args, base, n);
        const rd = r.traceReadEx(chunkBuf, want) as number;
        if (rd > 0) return { ret: rd, retries: attempt };
        Thread.sleep(0.001);
    }
    return { ret: -1, retries: 4 };
}

export interface ReadFramesArgs {
    first: number;
    count: number;
    samplesPerFrame: number;   // per-channel sample count (= :ACQ:MDEP)
    channels: number[];        // 1-indexed channels to deinterleave, in output order
    cropLo: number;
    cropHi: number;
    average: number;      // k; 1 = raw multi-frame
    outBits: number;      // raw payload width: 16 (u16), 12 (packed 2->3 B), or 8 (u8)
}

export interface ReadFramesResult {
    ret: number;
    elapsedTotalMs: number;
    dmaMs: number;
    dmaBytes: number;     // raw engine bytes DMA'd (eng*2 per frame) — pre-wire
    retries: number;
    failOffset: number;
    chunk: number;
    hwMaxFrameCount: number;
    framesDone: number;   // frames (raw) or averaged traces (avg) shipped
    bytes: number;
    err: number;
    k: number;
}

// Bracket every readback in export mode. Entering disables the live playback
// loop (SetPlayEnable(0) + ExportInit) so the replay engine can be driven;
// exiting MUST restore it (ExportBack + SetPlayEnable(1)) or the scope's
// acquisition/UI stays frozen until a reboot.
let inExport = false;

function enterExport(r: ResolvedFns): void {
    if (inExport) return;
    r.setPlayEnable(0);
    r.exportInit(0);
    inExport = true;
}

function exitExport(r: ResolvedFns): void {
    if (!inExport) return;
    r.exportBack();
    r.setPlayEnable(1);
    inExport = false;
}

/** dispose() safety net: restore the playback loop if a readback was cut short
 *  (host crash / disconnect) before its finally ran. No-op outside export. */
export function restoreExport(): void {
    if (!inExport) return;
    try {
        const r = ensureResolved();
        r.exportBack();
        r.setPlayEnable(1);
    } catch { /* resolver gone — nothing to restore against */ }
    inExport = false;
}

/** Unified readback. average=1 → raw multi-frame (int16 frames); average=k →
 *  in-agent averages (float32). Streams <u32 n><payload> per output trace over the
 *  data socket; returns status. Always restores the live playback loop. */
export function readFrames(a: ReadFramesArgs): ReadFramesResult {
    const r = ensureResolved();
    if (!acceptOnce()) throw new Error("native accept failed (client not connected)");
    enterExport(r);
    try {
        return readFramesInner(a, r);
    } finally {
        exitExport(r);   // restore the live loop even if the readback threw
    }
}

function readFramesInner(a: ReadFramesArgs, r: ResolvedFns): ReadFramesResult {
    const cm = ensureCModule();

    // Resolve the live interleave layout + each requested channel's lane offset.
    // The host sends only channel numbers + the per-channel sample count; the
    // agent derives stride, offsets, and the engine frame length here.
    const lay = channelLayout();
    const s = (lay.stride | 0) < 1 ? 1 : (lay.stride | 0);
    const spf = a.samplesPerFrame | 0;
    const eng = spf * s;
    const channels = a.channels.map((c) => c | 0);
    const nch = channels.length;
    if (nch < 1) throw new Error("readFrames: no channels requested");
    const offsets: number[] = channels.map((c) => {
        const o = chanOffsetWithinEnabled(c, lay.enabledMask, s);
        if (o < 0) throw new Error(
            `channel ${c} not enabled (enabled=${JSON.stringify(lay.enabledList)})`);
        return o;
    });
    const offsetsBuf = Memory.alloc(nch * 4);
    offsets.forEach((o, i) => offsetsBuf.add(i * 4).writeU32(o));

    const total = a.count | 0;
    const k = (a.average | 0) < 1 ? 1 : (a.average | 0);
    const scope = r.drvGetScope() as NativePointer;

    const { chunk, hwMax } = clampChunk(scope, r.profile.offsets, total);
    // Emit before the first blocking read so a wedged DMA still logs the chunk.
    send({ type: "fastrec_chunk", msg: `chunk=${chunk} cap=${hwMax} ` +
          `total=${total} k=${k} eng=${eng} nch=${nch}` });

    const outMax = spf;   // per-channel samples = eng / stride
    const cBytes = chunk * eng * 2;
    if (chunkBytes < cBytes) { chunkBuf = Memory.alloc(cBytes + 64); chunkBytes = cBytes; }

    const avg = k > 1;
    // One wire record holds one channel's row; the per-channel accumulator (avg)
    // persists across chunks since a k-group can span them, so size it × nch.
    let frameCap: number;
    if (avg) {
        const aBytes = outMax * 4 * nch;
        if (accumBytes < aBytes) { accumBuf = Memory.alloc(aBytes); accumBytes = aBytes; }
        if (fillBuf === null) fillBuf = Memory.alloc(4);
        const outCap = 4 + outMax * 4 + 64;
        if (avgOutBytes < outCap) { avgOutBuf = Memory.alloc(outCap); avgOutBytes = outCap; }
        fillBuf.writeU32(0);
        frameCap = outCap;
    } else {
        frameCap = 4 + outMax * 2 + 64;
        if (frameBytes < frameCap) { frameBuf = Memory.alloc(frameCap); frameBytes = frameCap; }
    }

    const args = primeArmArgs(r, a.first, eng);
    cm.gState.add(ST_ERR * 8).writeS64(0);
    cm.gState.add(ST_FRAMES * 8).writeS64(0);
    cm.gState.add(ST_BYTES * 8).writeS64(0);

    const t0 = Date.now();
    let dmaMs = 0, dmaBytes = 0, retries = 0, failOffset = -1, ret = 0;
    for (let off = 0; off < total; off += chunk) {
        const n = Math.min(chunk, total - off);
        const base = (a.first | 0) + off;
        const d0 = Date.now();
        const rc = readChunk(r, scope, args, base, n, n * eng * 2);
        dmaMs += Date.now() - d0;
        if (rc.ret > 0) dmaBytes += n * eng * 2;
        retries += rc.retries;
        if (rc.ret <= 0) { ret = -10; failOffset = base; cm.gState.add(ST_ERR * 8).writeS64(-10); break; }
        const wr = avg
            ? cm.sendAveragedFrames(chunkBuf!, n, eng, s, offsetsBuf, nch,
                          a.cropLo | 0, a.cropHi | 0,
                          k, accumBuf!, fillBuf!, avgOutBuf!, frameCap) as number
            : cm.sendFrames(chunkBuf!, n, eng, s, offsetsBuf, nch,
                            a.cropLo | 0, a.cropHi | 0, (a.outBits | 0) || 16,
                            frameBuf!, frameCap) as number;
        if (wr < 0) { ret = wr; break; }
    }
    const rd = (i: number): number => cm.gState.add(i * 8).readS64().toNumber();
    return {
        ret, elapsedTotalMs: Date.now() - t0, dmaMs, dmaBytes, retries, failOffset,
        chunk, hwMaxFrameCount: hwMax,
        framesDone: rd(ST_FRAMES), bytes: rd(ST_BYTES), err: rd(ST_ERR), k,
    };
}
