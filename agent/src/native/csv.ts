// Let Rigol's own CSV writer export WaveRecord memory. The documented SCPI
// memory-save handler hard-codes source=1 (Memory); its UI uses 2 (Record).
// Redirect that ONE source selection, observe the real Record writer, and
// detach on completion/timeout/dispose. No samples or scaling are synthesized.
import { ensureResolved } from "./resolve.js";

let sourceHook: InvocationListener | null = null;
let writerHook: InvocationListener | null = null;
let expiry: ReturnType<typeof setTimeout> | null = null;
let state = { armed: false, redirected: 0, started: 0, finished: 0, expired: false };

export function disarmRecordCsv(): typeof state {
    sourceHook?.detach(); sourceHook = null;
    writerHook?.detach(); writerHook = null;
    if (expiry !== null) clearTimeout(expiry);
    expiry = null;
    state.armed = false;
    return { ...state };
}

export function armRecordCsv(timeoutMs: number): typeof state {
    if (state.armed) throw new Error("CSV export already armed");
    if (!Number.isFinite(timeoutMs) || timeoutMs < 1000 || timeoutMs > 300000)
        throw new Error("CSV timeout must be 1000..300000 ms");
    const r = ensureResolved(); // firmware whitelist before touching native code
    const setter = r.module.getExportByName(r.profile.symbols.storageSetWaveDepth);
    const writer = r.module.getExportByName(r.profile.symbols.saveRecordAsCsv);
    state = { armed: true, redirected: 0, started: 0, finished: 0, expired: false };
    try {
        sourceHook = Interceptor.attach(setter, {
            onEnter(args) {
                if (!state.armed || state.redirected || args[1].toInt32() !== 1) return;
                args[1] = ptr(2);
                state.redirected++;
                sourceHook?.detach(); sourceHook = null;
            },
        });
        writerHook = Interceptor.attach(writer, {
            onEnter() { if (state.redirected === 1) state.started++; },
            onLeave() {
                if (state.started === 1) {
                    state.finished++;
                    disarmRecordCsv();
                }
            },
        });
        expiry = setTimeout(() => { state.expired = true; disarmRecordCsv(); }, timeoutMs);
        Interceptor.flush();
    } catch (e) { disarmRecordCsv(); throw e; }
    return { ...state };
}

export function recordCsvStatus(): typeof state { return { ...state }; }
