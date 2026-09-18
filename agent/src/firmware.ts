// Firmware profile + supported-firmware whitelist.
//
// Every native offset/symbol the agent uses is specific to one build of
// libscope-auklet.so. We fingerprint the loaded module against this whitelist
// and REFUSE to run on anything we don't recognize — see resolve.ts. Adding a
// new firmware means adding a validated entry here; nothing runs against an
// unprofiled binary.

export const AUKLET_LIB = "libscope-auklet.so";

/** A device + firmware build we have validated, with everything needed to
 *  resolve symbols and verify identity. */
export interface FirmwareProfile {
    /** *IDN? model field, e.g. "MHO98". */
    readonly model: string;
    /** *IDN? firmware version field, e.g. "00.01.00". The host reads *IDN? and
     *  passes (model, fwVersion) to resolve(); the agent checks this pair —
     *  no .so version read (those proved unreadable/slow). */
    readonly fwVersion: string;
    /** Ghidra image base the offsets/symbols below are relative to. */
    readonly imageBase: string;
    /** Native symbols the readback path resolves (by export name). */
    readonly symbols: Readonly<Record<string, string>>;
    /** Struct field offsets we read directly. */
    readonly offsets: Readonly<Record<string, number>>;
}

// Symbols the WaveRecord readback + channel-layout paths depend on. Mangled
// C++ names are the exact exports; the Dev* ones are plain C exports.
const MHO98_SYMBOLS = {
    // replay engine
    getScope:         "_Z12Drv_GetScopev",
    setRun:           "DevSystemScu_SetRun",
    analyzeRead:      "DevAnalyzeTrace_Read",
    stopScope:        "_ZN9CDrvScope9StopScopeEb",
    setPlyCurr:       "_Z20DrvRecord_SetPlyCurrj",
    setPlyBase:       "_Z20DrvRecord_SetPlyBasej",
    // single-frame export, hooked once to capture the SPU/SetRun args
    exportRecordData: "_Z28DrvWaveform_ExportRecordDatajRjPt",
    // export-mode enter/exit — bracket every readback. Entering disables the
    // live playback loop so the engine can be driven for replay; the exit MUST
    // restore it, or the scope's acquisition/UI stays frozen until a reboot.
    setPlayEnable:    "_Z24DrvAcquire_SetPlayEnableb",
    exportInit:       "_Z22DrvWaveform_ExportInitj",
    exportBack:       "_Z22DrvWaveform_ExportBackv",
    // streaming: agent-driven segment capture. getPlayInfo(count*, ready*)
    // reports how many frames the FPGA has recorded so far + a ready bit,
    // so the stream loop can pace itself without the SCPI WaveRecord host path.
    getPlayInfo:      "DevSystemScu_getPlayInfo",
    // SPU replay-setup setters (re-issued per chunk with the captured args)
    setWaveRange:     "DevAcquireSPU_SetWaveRange",
    setTxInfo:        "DevAcquireSpu_SetTxInfo",
    txFrmHead:        "DevAcquireSPU_TxFrmHead",
    setProcChEn:      "DevSystemSCU_SetProcChEn",
    setProcLaEn:      "DevSystemSCU_SetProcLaEn",
    setIntxOut:       "DevLaDisplayWpu_SetIntxOut",
    // channel layout
    getChanCount:     "_Z16API_GetChanCountv",
    scopeChanGetCH:   "_ZN10CScopeChan5getCHE4Chan",
} as const;

const MHO98_OFFSETS = {
    // CDrvParam = Drv_GetScope() + drvParam.
    drvParam: 0x5a00,
    // CDrvParam::dwMaxFrameCount — per-SetRun replay cap (read live, never trust
    // stale). At CDrvParam + dwMaxFrameCount.
    dwMaxFrameCount: 0x60,
    // CDrvParam::GetChanCount() — FPGA interleave stride. At CDrvParam + chanCount.
    chanCount: 0x80,
    // CScopeChan::getOnOff — (byte)*(this + chanOnOff) & 1.
    chanOnOff: 0x08,
} as const;

/** Validated firmware builds, keyed by the *IDN? (model, fwVersion). */
export const FIRMWARE_WHITELIST: readonly FirmwareProfile[] = [
    {
        model: "MHO98",
        fwVersion: "00.01.00",
        imageBase: "0x00100000",
        symbols: MHO98_SYMBOLS,
        offsets: MHO98_OFFSETS,
    },
];

/** The (model, fwVersion) the host parsed from *IDN? and passed to resolve(). */
export interface Fingerprint {
    readonly model: string;
    readonly fwVersion: string;
}

/** Pure, Frida-free identity match — unit-testable offline.
 *  Returns the matching profile or null (→ caller throws UnsupportedFirmware).
 *  Fail-closed: empty/unknown (model, fwVersion) never matches. */
export function matchProfile(fp: Fingerprint): FirmwareProfile | null {
    if (!fp.model || !fp.fwVersion) return null;
    for (const p of FIRMWARE_WHITELIST) {
        if (p.model === fp.model && p.fwVersion === fp.fwVersion) return p;
    }
    return null;
}
