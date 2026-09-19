// rigol-fastrec Frida agent — entry point.
//
// Targets the Rigol MHO900 series only and fails closed on any other firmware.
// frida-compile bundles this module tree into python/rigol_fastrec/_agent.js.

import {
    init, dispose, resolve, info, selftest,
    channelLayout, readbackOpen, readbackClose, readbackConnected, readFrames,
    streamFrames, streamStop,
} from "./rpc.js";

import { armRecordCsv, recordCsvStatus, disarmRecordCsv } from "./native/csv.js";

rpc.exports = {
    armRecordCsv, recordCsvStatus, disarmRecordCsv,
    init,
    dispose,
    resolve,
    info,
    selftest,
    channelLayout,
    readbackOpen,
    readbackClose,
    readbackConnected,
    readFrames,
    streamFrames,
    streamStop,
};
