// TCP data-socket lifecycle for the streaming readback.
//
// Socket create/bind/listen/accept are done with SystemFunction (captures
// errno) rather than in C, where the failures surface opaquely. JS owns the
// listen fd; the C hot loop (cmodule.ts) writes to the accepted client fd (its
// number lives in g_state[ST_CLIENTFD], set by fastrec_accept). The fd is
// process-wide, so the C loop can write to a socket JS accepted.
//
// accept uses the C raw-accept4 stub (cmodule fastrec_accept), not a SystemFunction:
// a blocking accept faults when Frida interrupts the parked thread, so the
// listen socket is non-blocking and accept4 returns the already-queued client.
//
// Behavior is only provable on-scope.

import { ensureCModule, findLibc, ST_CLIENTFD } from "./native/cmodule.js";

// Linux/arm64 ABI constants.
const AF_INET = 2;
const SOCK_STREAM = 1;
const SOCK_NONBLOCK = 0x800;
const SOL_SOCKET = 1;
const SO_REUSEADDR = 2;

// SystemFunction's generics are native type *descriptors* ("int"/"pointer"),
// and specific-vs-loose assignability is a contravariance headache; this is the
// socket layer, so <any, any> is the pragmatic, still-callable choice.
interface SysFns {
    socket: SystemFunction<any, any>;
    bind: SystemFunction<any, any>;
    listen: SystemFunction<any, any>;
    setsockopt: SystemFunction<any, any>;
    close: SystemFunction<any, any>;
}

let sys: SysFns | null = null;
let oneBuf: NativePointer | null = null;
let saBuf: NativePointer | null = null;
let listenFd = -1;
let clientFd = -1;

function htons16(p: number): number {
    return ((p & 0xff) << 8) | ((p >> 8) & 0xff);
}

function errnoOf(r: SystemFunctionResult<number>): number {
    return (r as UnixSystemFunctionResult<number>).errno;
}

function ensureSys(): SysFns {
    if (sys !== null) return sys;
    const EX = { scheduling: "exclusive" } as const;
    const built: SysFns = {
        socket: new SystemFunction(findLibc("socket"), "int", ["int", "int", "int"], EX),
        bind: new SystemFunction(findLibc("bind"), "int", ["int", "pointer", "uint"], EX),
        listen: new SystemFunction(findLibc("listen"), "int", ["int", "int"], EX),
        setsockopt: new SystemFunction(
            findLibc("setsockopt"), "int", ["int", "int", "int", "pointer", "uint"], EX),
        close: new SystemFunction(findLibc("close"), "int", ["int"], EX),
    };
    oneBuf = Memory.alloc(4);
    oneBuf.writeInt(1);
    saBuf = Memory.alloc(16);
    sys = built;
    return built;
}

export interface ListenResult {
    ok: boolean;
    stage?: string;
    errno?: number;
    listenfd?: number;
}

/** Open a non-blocking listening socket on `port` (INADDR_ANY). Closes any
 *  previous client/listen fds first. */
export function listen(port: number): ListenResult {
    const s = ensureSys();
    closeFds();

    let r = s.socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
    if (r.value < 0) return { ok: false, stage: "socket", errno: errnoOf(r) };
    const fd = r.value;
    s.setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, oneBuf!, 4);

    // sockaddr_in: family(2) port(2,net) addr(4,net=INADDR_ANY) zero(8).
    saBuf!.writeU16(AF_INET);
    saBuf!.add(2).writeU16(htons16(port | 0));
    saBuf!.add(4).writeU32(0);
    saBuf!.add(8).writeByteArray([0, 0, 0, 0, 0, 0, 0, 0]);

    r = s.bind(fd, saBuf!, 16);
    if (r.value < 0) { tryClose(fd); return { ok: false, stage: "bind", errno: errnoOf(r) }; }
    r = s.listen(fd, 1);
    if (r.value < 0) { tryClose(fd); return { ok: false, stage: "listen", errno: errnoOf(r) }; }
    listenFd = fd;
    return { ok: true, listenfd: fd };
}

/** Accept the (already-queued) client once. Returns true on success. */
export function acceptOnce(): boolean {
    if (listenFd < 0) return false;
    if (clientFd >= 0) return true;
    const c = ensureCModule().accept(listenFd);   // C raw accept4; sets g_state
    if (c >= 0) { clientFd = c; return true; }
    return false;   // -EAGAIN: client not queued yet
}

export function connected(): boolean {
    return clientFd >= 0;
}

function tryClose(fd: number): void {
    try { sys?.close(fd); } catch { /* ignore */ }
}

function closeFds(): void {
    if (clientFd >= 0) { tryClose(clientFd); clientFd = -1; }
    if (listenFd >= 0) { tryClose(listenFd); listenFd = -1; }
}

/** Close both fds and clear the C-side client fd (idempotent; safe from
 *  dispose() / fault paths). */
export function close(): void {
    closeFds();
    try { ensureCModule().gState.add(ST_CLIENTFD * 8).writeS64(-1); } catch { /* ignore */ }
}
