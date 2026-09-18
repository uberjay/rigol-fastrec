// The agent RPC surface. Thin wrappers over the core functions; the host calls
// these via Frida's rpc (camelCase here ↔ snake_case in Python).

import { native, resetState } from "./state.js";
import { resolveDiagnostic, type ResolveResult } from "./native/resolve.js";
import { NEON_BLOB_SIZE, ensureCModule, buildRawSyscall } from "./native/cmodule.js";
import { channelLayout as getChannelLayout, type ChannelLayout } from "./native/layout.js";
import { readFrames as doReadFrames, restoreExport, streamFrames as doStreamFrames, streamStop as doStreamStop, type ReadFramesArgs, type ReadFramesResult, type StreamArgs } from "./native/readback.js";
import * as transport from "./transport.js";

let initialized = false;

export function init(): { ok: boolean } {
    initialized = true;
    return { ok: true };
}

export function dispose(): { ok: boolean } {
    doStreamStop();    // ask any running stream loop to stop
    restoreExport();   // un-freeze the live playback loop if a readback was cut short
    transport.close();
    resetState();
    initialized = false;
    return { ok: true };
}

/** Check the host-supplied *IDN? (model, fwVersion) and resolve symbols.
 *  Throws UnsupportedFirmware if the firmware isn't whitelisted. */
export function resolve(model: string, fwVersion: string): ResolveResult {
    return resolveDiagnostic(model, fwVersion);
}

/** Live channel layout: stride + enabled channels + per-channel offsets. */
export function channelLayout(): ChannelLayout {
    return getChannelLayout();
}

/** Open the streaming data socket on `port`. */
export function readbackOpen(port: number): transport.ListenResult {
    return transport.listen(port | 0);
}

export function readbackClose(): { ok: boolean } {
    transport.close();
    return { ok: true };
}

export function readbackConnected(): { connected: boolean } {
    return { connected: transport.connected() };
}

/** Unified multi-frame readback; streams over the data socket, returns status. */
export function readFrames(args: ReadFramesArgs): ReadFramesResult {
    return doReadFrames(args);
}

/** Start the agent-driven continuous stream (fire-and-forget; returns at once).
 *  Frames flow over the data socket until streamStop(). */
export function streamFrames(args: StreamArgs): { ok: boolean; error?: string } {
    return doStreamFrames(args);
}

/** Stop the running stream after the current batch. */
export function streamStop(): { ok: boolean } {
    return doStreamStop();
}

/** Smoke test: build the CModule and exercise the trivial ping fn + the
 *  raw_syscall stub (getpid). Confirms the CModule is callable (i.e. retained,
 *  not GC-freed) and the svc stub works, before a real readback. */
export function selftest(): Record<string, string> {
    const out: Record<string, string> = {};
    try {
        const rs = buildRawSyscall();
        const f = new NativeFunction(rs, "long", ["long", "long", "long", "long", "long"],
            { scheduling: "exclusive" });
        out.rawSyscallGetpid = String(f(172, 0, 0, 0, 0));
    } catch (e) { out.rawSyscallGetpid = "ERR:" + e; }
    try {
        const cm = ensureCModule();
        out.gState = cm.gState.toString();
        out.ping = String(cm.ping(20));         // 41
        out.getpid = String(cm.testGetpid());   // the scope pid
    } catch (e) {
        out.cmodule = "ERR:" + e;
    }
    return out;
}

export function info(): {
    initialized: boolean;
    resolved: boolean;
    model: string | null;
    fwVersion: string | null;
    neonBlobSize: number;
    connected: boolean;
} {
    return {
        initialized,
        resolved: native.module !== null,
        model: native.profile?.model ?? null,
        fwVersion: native.profile?.fwVersion ?? null,
        neonBlobSize: NEON_BLOB_SIZE,
        connected: transport.connected(),
    };
}
