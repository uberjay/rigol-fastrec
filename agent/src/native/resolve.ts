// Fingerprint-first symbol resolution.
//
// ensureResolved() ALWAYS verifies the loaded libscope-auklet.so against the
// supported-firmware whitelist BEFORE building any NativeFunction. On a
// mismatch it throws UnsupportedFirmware and resolves nothing — every native
// offset/symbol is firmware-specific, so an unknown binary is a hard stop.
//
// Only the WaveRecord readback + channel-layout symbols are resolved.
// NativeFunctions are typed loosely (<any, any>): the descriptor-generic form
// is a contravariance headache and these are internal call wrappers.

import { native } from "../state.js";
import {
    AUKLET_LIB,
    matchProfile,
    type Fingerprint,
    type FirmwareProfile,
} from "../firmware.js";

export class UnsupportedFirmware extends Error {
    constructor(readonly fingerprint: Fingerprint | null, detail: string) {
        super(`UnsupportedFirmware: ${detail}`);
        this.name = "UnsupportedFirmware";
    }
}

/** Resolved native handles for the readback + layout paths. Loose types: these
 *  are internal call wrappers, called with the right ABI by readback.ts. */
export interface ResolvedFns {
    module: Module;
    profile: FirmwareProfile;
    // addresses hooked (Interceptor) by the SPU/SetRun arg-priming
    setRunAddr: NativePointer;
    setWaveRangeAddr: NativePointer;
    setTxInfoAddr: NativePointer;
    // replay engine
    drvGetScope: NativeFunction<any, any>;       // () -> CDrvScope*
    setRun: NativeFunction<any, any>;            // 9-arg SetRun
    traceReadEx: NativeFunction<any, any>;       // (buf, want) exclusive
    stopScope: NativeFunction<any, any>;         // (scope, u8)
    setPlyCurr: NativeFunction<any, any>;        // (u32)
    setPlyBase: NativeFunction<any, any>;        // (u32)
    exportRecordData: NativeFunction<any, any>;  // (u32, count*, buf*)
    // export-mode enter/exit — bracket the readback; restore the live loop
    setPlayEnable: NativeFunction<any, any>;     // (u8 enable)
    exportInit: NativeFunction<any, any>;        // (u32 flag) -> int
    exportBack: NativeFunction<any, any>;        // () -> int
    getPlayInfo: NativeFunction<any, any>;       // (count*, ready*) -> int (streaming)
    // SPU replay setters
    setWaveRange: NativeFunction<any, any>;      // (6 x u32)
    setTxInfo: NativeFunction<any, any>;         // (2 x u32)
    txFrmHead: NativeFunction<any, any>;         // (u32)
    setProcChEn: NativeFunction<any, any>;       // (u32)
    setProcLaEn: NativeFunction<any, any>;       // (u32)
    setIntxOut: NativeFunction<any, any>;        // (3 x u32)
    // channel layout
    apiGetChanCount: NativeFunction<any, any>;   // () -> int
    scopeChanGetCH: NativeFunction<any, any>;    // (int) -> CScopeChan*
}

let resolved: ResolvedFns | null = null;
let lastFingerprint: Fingerprint | null = null;

function findSym(module: Module, name: string): NativePointer {
    const a = module.findExportByName(name) ?? Module.findExportByName(null, name);
    if (a === null || a.isNull()) {
        throw new UnsupportedFirmware(
            lastFingerprint,
            `symbol ${name} not found in ${AUKLET_LIB} — firmware profile is ` +
            `wrong for this build.`);
    }
    return a;
}

/** Check the host-supplied *IDN? (model, fwVersion), then build the readback
 *  symbols. Cached after the first (checked) call; later calls (from readback /
 *  layout) need no args. Throws UnsupportedFirmware if the firmware isn't
 *  whitelisted, or if called before a checked resolve(). */
export function ensureResolved(model?: string, fwVersion?: string): ResolvedFns {
    if (resolved !== null) return resolved;

    const module = Process.findModuleByName(AUKLET_LIB);
    if (module === null) {
        throw new UnsupportedFirmware(
            null, `${AUKLET_LIB} not loaded — is this a Rigol MHO900 scope?`);
    }
    if (model === undefined || fwVersion === undefined) {
        throw new Error("resolve(model, fwVersion) must be called before readback");
    }
    const fp: Fingerprint = { model, fwVersion };
    lastFingerprint = fp;
    const profile = matchProfile(fp);
    if (profile === null) {
        throw new UnsupportedFirmware(
            fp,
            `unrecognized firmware ${model}/${fwVersion}. Targets MHO900 series ` +
            `only; add a validated profile entry to firmware.ts.`);
    }

    const sym = profile.symbols;
    const nf = (name: string, ret: NativeFunctionReturnType,
                args: NativeFunctionArgumentType[],
                opts?: NativeFunctionOptions): NativeFunction<any, any> =>
        new NativeFunction(findSym(module, sym[name]), ret, args, opts);
    const EX: NativeFunctionOptions = { scheduling: "exclusive" };

    resolved = {
        module,
        profile,
        setRunAddr: findSym(module, sym["setRun"]),
        setWaveRangeAddr: findSym(module, sym["setWaveRange"]),
        setTxInfoAddr: findSym(module, sym["setTxInfo"]),
        drvGetScope: nf("getScope", "pointer", []),
        setRun: nf("setRun", "int",
            ["uint32", "int32", "uint64", "int32", "uint32", "int32", "int32",
             "uint64", "uint64"]),
        traceReadEx: nf("analyzeRead", "int", ["pointer", "uint32"], EX),
        stopScope: nf("stopScope", "int", ["pointer", "uint8"]),
        setPlyCurr: nf("setPlyCurr", "void", ["uint32"]),
        setPlyBase: nf("setPlyBase", "void", ["uint32"]),
        exportRecordData: nf("exportRecordData", "int", ["uint32", "pointer", "pointer"]),
        setPlayEnable: nf("setPlayEnable", "void", ["uint8"]),
        exportInit: nf("exportInit", "int", ["uint32"]),
        exportBack: nf("exportBack", "int", []),
        getPlayInfo: nf("getPlayInfo", "int", ["pointer", "pointer"]),
        setWaveRange: nf("setWaveRange", "void",
            ["uint32", "uint32", "uint32", "uint32", "uint32", "uint32"]),
        setTxInfo: nf("setTxInfo", "void", ["uint32", "uint32"]),
        txFrmHead: nf("txFrmHead", "void", ["uint32"]),
        setProcChEn: nf("setProcChEn", "void", ["uint32"]),
        setProcLaEn: nf("setProcLaEn", "void", ["uint32"]),
        setIntxOut: nf("setIntxOut", "void", ["uint32", "uint32", "uint32"]),
        apiGetChanCount: nf("getChanCount", "int", []),
        scopeChanGetCH: nf("scopeChanGetCH", "pointer", ["int32"]),
    };
    native.module = module;
    native.profile = profile;
    return resolved;
}

/** Diagnostic returned by the resolve() RPC. */
export interface ResolveResult {
    ok: boolean;
    imageBase: string;
    model: string;
    fwVersion: string;
    fingerprint: Fingerprint;
}

export function resolveDiagnostic(model: string, fwVersion: string): ResolveResult {
    const r = ensureResolved(model, fwVersion);
    return {
        ok: true,
        imageBase: r.profile.imageBase,
        model: r.profile.model,
        fwVersion: r.profile.fwVersion,
        fingerprint: lastFingerprint!,
    };
}
