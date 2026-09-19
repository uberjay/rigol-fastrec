// Opt-in second pass: replay small prefixes with headers, preserving the normal
// waveform DMA path. Called while readback owns export mode.
import type { ResolvedFns } from "./resolve.js";
import { decodeTimestampChunk } from "./timestamp_decode.js";

export interface TimestampArmArgs {
    wave: number[];
    tx: number[];
    mask: number;
    recordLen: UInt64;
    interval: UInt64;
    p9: UInt64;
}

export interface TimestampResult {
    frameTimestampTicks: string[];
    timestampFirstFrame: number;
    timestampTickFs: number;
    timestampBits: number;
    timestampSource: string;
    timestampElapsedMs: number;
    timestampDmaBytes: number;
    timestampDmaMs: number;
    timestampRetries: number;
    timestampFallbackFrames: number;
}

// dispose() can also restore the transfer geometry after an interrupted RPC.
let restoreGeometry: (() => void) | null = null;
export function restoreTimestampGeometry(): void {
    if (restoreGeometry === null) return;
    restoreGeometry();
    restoreGeometry = null;
}

export function readFrameTimestamps(r: ResolvedFns, scope: NativePointer,
        args: TimestampArmArgs, first: number, count: number, stride: number,
        chunk: number): TimestampResult {
    const begin = Date.now();
    const samples = 32;
    if (args.tx[1] < samples || args.wave[3] < args.tx[1])
        throw new Error("timestamp prefixes require at least 32 samples per channel");
    const frameBytes = samples * stride * 2 + 16;
    const buffer = Memory.alloc(chunk * frameBytes);
    const tagOut = Memory.alloc(8);
    const getTag = new NativeFunction(r.module.getExportByName(r.profile.symbols.getRecordTag),
                                     "int", ["pointer"]);
    const interval = uint64("10000000000"); // 10 us, only during timestamp replay
    const wave = args.wave.slice();
    wave[3] = args.wave[3] - args.tx[1] + samples;
    let dmaMs = 0, dmaBytes = 0, retries = 0, fallbackFrames = 0;
    const ticks: string[] = [];
    restoreGeometry = () => {
        r.setWaveRange(...args.wave);
        r.setTxInfo(...args.tx);
    };
    function replay(base: number, n: number): string {
        for (let attempt = 0; attempt < 4; attempt++) {
            r.setPlyCurr(base); r.setPlyBase(base);
            r.setProcChEn(1); r.setProcLaEn(0);
            r.setWaveRange(...wave); r.setTxInfo(args.tx[0], samples);
            r.txFrmHead(0);
            r.stopScope(scope, 1);
            r.setRun(args.mask, 4, args.recordLen, n, base, base, base + n - 1,
                     interval, args.p9);
            r.setIntxOut(0, 0, 0);
            const t = Date.now();
            const rd = r.traceReadEx(buffer, n * frameBytes) as number;
            dmaMs += Date.now() - t;
            if (rd === n * frameBytes) {
                dmaBytes += rd;
                // This wrapper returns zero unconditionally; its output is
                // validated by header identity, register anchors and ordering.
                getTag(tagOut);
                const words = new Uint16Array(buffer.readByteArray(n * frameBytes)!);
                for (let i = 0; i < n; i++) {
                    const h = i * frameBytes / 2;
                    if (words[h] !== 0xfa05 || words[h + 4] !== ((base + i) & 0xffff))
                        throw new Error(`timestamp header identity mismatch at frame ${base + i}`);
                }
                return tagOut.readU64().toString();
            }
            if (attempt < 3) { retries++; Thread.sleep(0.001); }
        }
        throw new Error(`timestamp DMA failed at frame ${base}`);
    }
    try {
        for (let offset = 0; offset < count; offset += chunk) {
            const base = first + offset, n = Math.min(chunk, count - offset);
            const start = replay(base, 1);
            const end = n === 1 ? start : replay(base, n);
            const bytes = buffer.readByteArray(n * frameBytes);
            if (bytes === null) throw new Error("timestamp buffer is unreadable");
            const decoded = decodeTimestampChunk(new Uint16Array(bytes), frameBytes / 2,
                                                 base, n, start, end);
            if (decoded !== null) { for (const tick of decoded) ticks.push(tick); }
            else {
                fallbackFrames += n;
                ticks.push(start);
                for (let i = 1; i < n - 1; i++) ticks.push(replay(base + i, 1));
                if (n > 1) ticks.push(end);
            }
        }
        for (let i = 1; i < ticks.length; i++) {
            if (BigInt(ticks[i]) <= BigInt(ticks[i - 1]))
                throw new Error(`nonincreasing timestamp at frame ${first + i}`);
        }
        return {frameTimestampTicks: ticks, timestampFirstFrame: first,
            timestampTickFs: 250000, timestampBits: 64, timestampSource: "prefix-register",
            timestampElapsedMs: Date.now() - begin, timestampDmaBytes: dmaBytes,
            timestampDmaMs: dmaMs, timestampRetries: retries,
            timestampFallbackFrames: fallbackFrames};
    } finally {
        restoreTimestampGeometry();
    }
}
