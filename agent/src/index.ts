// rigol-fastrec Frida agent — entry point.
//
// Targets the Rigol MHO900 series only and fails closed on any other firmware.
// frida-compile bundles this module tree into python/rigol_fastrec/_agent.js.

import {
    init, dispose, resolve, info, selftest,
    channelLayout, readbackOpen, readbackClose, readbackConnected, readFrames,
} from "./rpc.js";

rpc.exports = {
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
};
