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
import { acceptOnce, connected as transportConnected } from "../transport.js";

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

// =====================================================================
// Continuous streaming (agent-driven).
//
// readFrames reads ONE bounded batch that the host already captured via the
// SCPI WaveRecord run. Streaming instead has the AGENT own the loop: it
// captures a batch itself (SetRun mode=2), waits for the FPGA to record it
// (getPlayInfo), replays + DMAs it to RAM, RE-ARMS THE NEXT CAPTURE, then does
// the blocking C send of the batch just read. Because the mode=2 arm returns
// immediately and the capture proceeds in hardware, the (slow) wire send of
// batch k overlaps the (trigger-paced) capture of batch k+1 — continuous flow
// with no host round-trip per batch and no socket re-architecture.
//
// Raw only (16/12/8-bit); averaging is not wired into the stream path.
// =====================================================================

let streaming = false;
let stopRequested = false;
let playCountOut: NativePointer | null = null;
let playReadyOut: NativePointer | null = null;

export interface StreamArgs {
    samplesPerFrame: number;   // per-channel sample count (= :ACQ:MDEP)
    channels: number[];        // 1-indexed channels to deinterleave, in output order
    cropLo: number;
    cropHi: number;
    outBits: number;           // raw payload width: 16 | 12 | 8
    batch: number;             // cap on frames per capture; <=0 → dwMaxFrameCount
                               // (the loop sizes each arm adaptively below it)
}

const sleep = (ms: number): Promise<void> =>
    new Promise((resolve) => setTimeout(resolve, ms));

// SetRun(mode=2): arm a capture of `n` frames into segment memory. Returns at
// once; the FPGA records as triggers arrive. Same args as the replay arm
// (armChunk) bar the mode + count, so capture and replay stay consistent.
function armCapture(r: ResolvedFns, scope: NativePointer, args: ArmArgs, n: number): void {
    r.stopScope(scope, 1);
    r.setRun(args.mask, 2, args.recordLen, n, 0, 0, n - 1, args.interval, args.p9);
}

function playInfo(r: ResolvedFns): { ready: number; count: number } {
    if (playCountOut === null) playCountOut = Memory.alloc(4);
    if (playReadyOut === null) playReadyOut = Memory.alloc(4);
    r.getPlayInfo(playCountOut, playReadyOut);
    return { ready: playReadyOut.readU32() & 1, count: playCountOut.readU32() | 0 };
}

// Low-volume telemetry for the first poll transitions of a stream, so the
// getPlayInfo semantics (ready bit, count) can be read off the host log.
let pollTraceLeft = 0;
function tracePoll(msg: string): void {
    if (pollTraceLeft <= 0) return;
    pollTraceLeft--;
    send({ type: "fastrec_stream", msg: "stream poll: " + msg });
}

// Poll getPlayInfo until the capture holds a full batch of n frames. The
// hardware reports ready as soon as the first frame lands, so returning on
// ready alone replays one frame per cycle and never batches. A partial batch
// is delivered instead once the count stops advancing for QUIET_MS (sparse
// triggers: don't hold a frame hostage waiting for n) or FILL_MS after the
// first frame landed (bounds latency at moderate rates). Before any frame
// lands, re-arm every PER_ARM_MS in case a hot re-arm missed the trigger edge.
// Yields between polls so streamStop is seen. Returns the frame count to replay
// (1..n) and whether the batch completed on its own; count 0 means abort
// (stop / disconnect / bail).
const FILL_MS = 50, QUIET_MS = 20;

async function waitCaptured(r: ResolvedFns, scope: NativePointer,
                            args: ArmArgs, n: number): Promise<{ count: number; full: boolean }> {
    const PER_ARM_MS = 200, BAIL_MS = 30000;
    const bailAt = Date.now() + BAIL_MS;
    let reArmAt = Date.now() + PER_ARM_MS;
    let seen = 0;                  // frames the hardware has reported so far
    let firstAt = 0, lastAt = 0;   // when the first frame landed / count last advanced
    while (!stopRequested && transportConnected()) {
        const pi = playInfo(r);
        const now = Date.now();
        if (pi.ready) {
            const c = pi.count < 1 ? 1 : pi.count;
            if (c > seen) {
                seen = c; lastAt = now;
                if (firstAt === 0) firstAt = now;
            }
            if (c >= n) { tracePoll(`full n=${n}`); return { count: n, full: true }; }
        }
        if (seen > 0) {
            if (now - lastAt >= QUIET_MS) {
                tracePoll(`quiet seen=${seen} n=${n}`); return { count: seen, full: false };
            }
            if (now - firstAt >= FILL_MS) {
                tracePoll(`fill seen=${seen} n=${n}`); return { count: seen, full: false };
            }
        } else if (now >= reArmAt) {
            armCapture(r, scope, args, n);
            reArmAt = now + PER_ARM_MS;
        }
        if (now > bailAt) return { count: 0, full: false };
        await sleep(0);
    }
    return { count: 0, full: false };
}

/** Start the continuous stream loop (fire-and-forget); returns at once. The
 *  host connects the data socket first, then reads records until it calls
 *  streamStop(). Frames use the same <u32 n><payload> record format as read. */
export function streamFrames(a: StreamArgs): { ok: boolean; error?: string } {
    if (streaming) return { ok: false, error: "stream already running" };
    streaming = true;
    stopRequested = false;
    streamLoop(a)
        .catch((e) => { send({ type: "fastrec_stream", msg: "stream error: " + e }); })
        .then(() => { streaming = false; });
    return { ok: true };
}

/** Ask the stream loop to finish the current batch and stop. */
export function streamStop(): { ok: boolean } {
    stopRequested = true;
    return { ok: true };
}

async function streamLoop(a: StreamArgs): Promise<void> {
    const r = ensureResolved();
    const cm = ensureCModule();

    // Host connects before calling streamFrames; pick up the queued connection.
    const connectAt = Date.now() + 10000;
    while (!acceptOnce()) {
        if (stopRequested || Date.now() > connectAt) {
            send({ type: "fastrec_stream", msg: "no client connected; stream aborted" });
            return;
        }
        await sleep(5);
    }

    const lay = channelLayout();
    const s = (lay.stride | 0) < 1 ? 1 : (lay.stride | 0);
    const spf = a.samplesPerFrame | 0;
    const eng = spf * s;
    const channels = a.channels.map((c) => c | 0);
    const nch = channels.length;
    if (nch < 1) throw new Error("streamFrames: no channels requested");
    const offsets = channels.map((c) => {
        const o = chanOffsetWithinEnabled(c, lay.enabledMask, s);
        if (o < 0) throw new Error(
            `channel ${c} not enabled (enabled=${JSON.stringify(lay.enabledList)})`);
        return o;
    });
    const offsetsBuf = Memory.alloc(nch * 4);
    offsets.forEach((o, i) => offsetsBuf.add(i * 4).writeU32(o));

    const scope = r.drvGetScope() as NativePointer;
    const hwMax = clampChunk(scope, r.profile.offsets, 1).hwMax;
    const N = a.batch > 0 ? Math.min(a.batch | 0, hwMax) : hwMax;

    const cBytes = N * eng * 2;
    if (chunkBytes < cBytes) { chunkBuf = Memory.alloc(cBytes + 64); chunkBytes = cBytes; }
    const outMax = spf;
    const fCap = 4 + outMax * 2 + 64;
    if (frameBytes < fCap) { frameBuf = Memory.alloc(fCap); frameBytes = fCap; }

    const args = primeArmArgs(r, 0, eng);
    const outBits = (a.outBits | 0) || 16;
    cm.gState.add(ST_ERR * 8).writeS64(0);
    cm.gState.add(ST_FRAMES * 8).writeS64(0);
    cm.gState.add(ST_BYTES * 8).writeS64(0);

    enterExport(r);
    pollTraceLeft = 40;
    send({ type: "fastrec_stream",
           msg: `stream start: batch=${N} cap=${hwMax} eng=${eng} nch=${nch} outBits=${outBits}` });
    try {
        // Adaptive arm size, 1..N. Stopping a capture mid-flight (to replay a
        // partial batch) stalls the engine for ~10-20 ms, while a capture that
        // completed on its own stops in ~1 ms. So size the arm to what the
        // trigger rate fills within FILL_MS: double while full batches land
        // quickly, and on a partial drop to below what arrived. Sparse
        // triggers settle at n=1 (no stall); fast ones grow toward N.
        let n = 1;
        let armedAt = Date.now();
        armCapture(r, scope, args, n);          // batch 0
        while (!stopRequested && transportConnected()) {
            const got = await waitCaptured(r, scope, args, n);
            const waitedMs = Date.now() - armedAt;
            if (got.count < 1) {
                armedAt = Date.now(); armCapture(r, scope, args, n); await sleep(1); continue;
            }
            // replay + DMA the captured batch into RAM
            armChunk(r, scope, args, 0, got.count);
            const rd = r.traceReadEx(chunkBuf!, got.count * eng * 2) as number;
            if (rd <= 0) {
                armedAt = Date.now(); armCapture(r, scope, args, n); await sleep(1); continue;
            }
            if (got.full) {
                // Grow only while the whole batch lands well inside FILL_MS
                // and frames arrive well inside QUIET_MS of each other, so
                // the doubled batch neither exceeds the fill window nor
                // trips the quiet timeout between frames.
                if (n < N && waitedMs < FILL_MS / 2 && waitedMs * 2 < QUIET_MS * n) {
                    n = Math.min(n * 2, N);
                }
            } else {
                n = Math.max(1, Math.min(N, Math.floor(got.count * 0.75)));
            }
            tracePoll(`arm n=${n} after ${got.full ? "full" : "partial"} ${got.count} in ${waitedMs} ms`);
            // segment memory is free now (DMA done): re-arm the NEXT capture so
            // the FPGA records batch k+1 while we do the blocking send of batch k.
            armedAt = Date.now();
            armCapture(r, scope, args, n);
            const wr = cm.sendFrames(chunkBuf!, got.count, eng, s, offsetsBuf, nch,
                a.cropLo | 0, a.cropHi | 0, outBits, frameBuf!, fCap) as number;
            if (wr < 0) break;     // client gone / write error
            await sleep(0);        // yield so streamStop / RPCs are serviced
        }
    } finally {
        exitExport(r);
    }
}
