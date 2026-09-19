"""Readback client (Frida).

`open()` attaches to the scope's Frida agent over the network, loads the
bundle, init()s, and resolve()s (which runs the firmware fingerprint check).
`read()` streams recorded frames back over the data socket; `close()` tears it
all down.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

from .agent import load_agent_source
from .timestamps import FrameTimestamps, validate_timestamp_request
from .exceptions import AgentError, ReadbackShortRead, ScopeNotFound, UnsupportedFirmware

log = logging.getLogger("rigol_fastrec.readback")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes or raise (the stream frames are fixed-length)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"data socket closed after {len(buf)}/{n} bytes")
        buf += chunk
    return bytes(buf)


def _unpack12(buf: bytes, n: int):
    """Unpack n 12-bit samples from `buf` (2 samples per 3 bytes, the agent's
    LE nibble layout) and reconstruct to uint16 in the 16-bit code domain (<<4),
    so the result is drop-in with the i16 path (incl. to_volts)."""
    import numpy as np
    raw = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 3).astype(np.uint16)
    b0, b1, b2 = raw[:, 0], raw[:, 1], raw[:, 2]
    s0 = b0 | ((b1 & 0x0F) << 8)
    s1 = (b1 >> 4) | (b2 << 4)
    vals = np.empty(raw.shape[0] * 2, dtype=np.uint16)
    vals[0::2] = s0
    vals[1::2] = s1
    return (vals[:n] << 4).astype(np.uint16)


class Readback:
    """Client for the Frida agent injected into the scope firmware."""

    def __init__(self, host: str, *, frida_port: int = 27042,
                 data_port: int = 5028, process: str = "RIGOL.SCOPE") -> None:
        self._host = host
        self._frida_port = frida_port
        self._data_port = data_port
        self._process = process
        self._device = None
        self._session = None
        self._script = None
        self._sock = None        # data socket; opened once, reused across reads
        self.resolved: dict | None = None
        self._last_read_stats: dict | None = None
        self._last_frame_timestamps: FrameTimestamps | None = None

    @property
    def last_read_stats(self) -> dict | None:
        """Agent telemetry for the last completed read, or None after a failure.

        Includes actual chunk size, hardware chunk capacity, frame/byte counts
        and DMA time. Returns a copy; no instrument query is performed.
        """
        return None if self._last_read_stats is None else dict(self._last_read_stats)

    @property
    def last_frame_timestamps(self) -> FrameTimestamps | None:
        """Timestamps from the last successful opt-in read; otherwise None."""
        return self._last_frame_timestamps

    def open(self, *, model: str | None = None,
             fw_version: str | None = None) -> "Readback":
        """Attach + load the agent + init + resolve. `model`/`fw_version` come
        from the host's ``*IDN?`` (ScpiControl) and check the agent against the
        supported-firmware whitelist; WaveRecorder supplies them. Without them
        the agent refuses (fail-closed)."""
        import frida
        # The scope's frida-server is reached as a remote device over LAN.
        try:
            self._device = frida.get_device_manager().add_remote_device(
                f"{self._host}:{self._frida_port}")
            procs = self._device.enumerate_processes()
        except Exception as e:   # frida errors subclass Exception (no common base)
            raise ScopeNotFound(
                f"can't reach frida-server at {self._host}:{self._frida_port}: "
                f"{type(e).__name__}: {e}") from e

        # Match the target process by name, with a fuzzy fallback so a renamed
        # build is found (and the error lists what's actually there).
        match = [p for p in procs if p.name == self._process]
        if not match:
            fuzzy = [p for p in procs if any(
                k in p.name.lower() for k in ("rigol", "scope", "auklet"))]
            seen = ", ".join(f"{p.name}(pid={p.pid})" for p in fuzzy) or "none"
            raise ScopeNotFound(
                f"process {self._process!r} not found; scope-like: {seen}")
        try:
            self._session = self._device.attach(match[0].pid)
        except Exception as e:   # frida errors subclass Exception (no common base)
            raise ScopeNotFound(
                f"attach to {self._process} failed: {type(e).__name__}: {e}") from e

        self._script = self._session.create_script(load_agent_source())
        self._script.on("message", self._on_message)
        self._script.load()
        self._script.exports_sync.init()
        try:
            # resolve(model, fwVersion) checks the agent against the *IDN? the
            # host validated, then resolves symbols.
            self.resolved = dict(self._script.exports_sync.resolve(model, fw_version))
        except Exception as e:  # frida surfaces the agent throw here
            msg = str(e)
            if "UnsupportedFirmware" in msg:
                raise UnsupportedFirmware(msg) from e
            raise AgentError(f"resolve() failed: {msg}") from e
        log.info("attached to %r (pid %d) on %s:%d; agent resolved (model=%s fw=%s)",
                 match[0].name, match[0].pid, self._host, self._frida_port,
                 model, fw_version)
        return self

    @staticmethod
    def _on_message(message: dict, data) -> None:
        """Frida script message sink. The agent emits per-readback telemetry via
        ``send({type:"fastrec_chunk", …})`` (chunk clamp / stride / nch) and any
        uncaught JS error arrives here too; without a handler frida discards both.
        Route them to the readback logger (chunk → DEBUG, errors → WARNING)."""
        kind = message.get("type")
        if kind == "send":
            payload = message.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "fastrec_chunk":
                log.debug("agent: %s", payload.get("msg"))
            else:
                log.debug("agent message: %r", payload)
        elif kind == "error":
            log.warning("agent JS error: %s",
                        message.get("stack") or message.get("description"))

    def arm_record_csv(self, timeout: float) -> dict:
        """Internal CSV source selection; scope must be exclusively owned."""
        return dict(self._script.exports_sync.arm_record_csv(timeout*1000))

    def record_csv_status(self) -> dict:
        return dict(self._script.exports_sync.record_csv_status())

    def disarm_record_csv(self) -> dict:
        return dict(self._script.exports_sync.disarm_record_csv())

    def channel_layout(self) -> dict:
        """Live channel layout: {stride, enabledList, apiChanCount, enabledMask}."""
        lay = dict(self._script.exports_sync.channel_layout())
        log.debug("channel_layout: stride=%s enabled=%s",
                  lay.get("stride"), lay.get("enabledList"))
        return lay

    def read(self, *, count: int, samples_per_frame: int,
             channels: list[int] | tuple[int, ...] = (1,),
             crop: tuple[int, int] | None = None, average: int = 1,
             sample_bits: int = 16, transport: str = "raw",
             first: int = 0, progress=None, timestamps: bool = False) -> dict:
        """Read `count` frames of each channel in `channels`, optionally cropped
        to a sample window and/or averaged in groups of `average`.

        `samples_per_frame` is the PER-CHANNEL sample count (= :ACQ:MDEP). The
        host sends only the channel numbers + this count; the agent reads the
        live interleave layout, deinterleaves every requested channel out of a
        single DMA pass, and streams one record per (group, channel).

        For raw reads (average == 1), `sample_bits` and `transport` pick the wire
        encoding:
          * 16 / "raw"    — full 16-bit codes → uint16.
          * 16 / "packed" — top 12 bits packed 2-samples-per-3-bytes (−25%),
            unpacked + shifted back to the 16-bit domain → uint16 (drop-in with
            to_volts).
          * 8 / "raw"     — top 8 bits (code>>8) → uint8 (−50%). half resolution;
            to_volts handles uint8 (lifts it back to the 16-bit domain ×256).
        "packed" requires sample_bits=16; both apply only to raw reads.

        ``timestamps=True`` adds a short-prefix replay pass and exposes one
        counter value per frame through ``last_frame_timestamps``. Requires
        ``average=1``. Disabled reads perform no timestamp replay.

        Returns ``{channel: ndarray}`` shaped (count // average, out_len):
        uint16 (16-bit) or uint8 (8-bit) when average == 1, else float32 averages.
        """
        import numpy as np

        self._last_read_stats = None
        self._last_frame_timestamps = None
        validate_timestamp_request(timestamps, average)
        # Validate encoding args up front (before touching the scope).
        if sample_bits not in (16, 8):
            raise ValueError(f"sample_bits must be 16 or 8, got {sample_bits}")
        if transport not in ("raw", "packed"):
            raise ValueError(f"transport must be 'raw' or 'packed', got {transport!r}")
        if sample_bits == 8 and transport == "packed":
            raise ValueError("transport='packed' applies only to sample_bits=16 "
                             "(8-bit samples are already byte-aligned)")
        if int(average) > 1 and (sample_bits != 16 or transport != "raw"):
            raise ValueError("sample_bits/transport apply only to raw "
                             "(average==1) reads; averaged reads are float32 averages")

        if self._script is None:
            raise ScopeNotFound("not open()ed")
        chans = [int(c) for c in channels]
        if not chans:
            raise ValueError("no channels to read")
        nch = len(chans)
        full = int(samples_per_frame)

        if crop is not None:
            crop_lo, crop_hi = int(crop[0]), int(crop[1])
            if not (0 <= crop_lo < crop_hi <= full):
                raise ValueError(
                    f"crop {(crop_lo, crop_hi)} out of range for "
                    f"{full} samples/frame (need 0 <= lo < hi <= {full})")
            out_len = crop_hi - crop_lo
        else:
            crop_lo = crop_hi = 0
            out_len = full

        k = max(1, int(average))
        n_out = count // k
        if n_out < 1:
            raise ValueError(f"count ({count}) < average ({k})")
        # average>1 → float32 averages. average==1 → 16-bit (raw/packed) or 8-bit.
        if k > 1:
            item, itemsize, out_bits = np.float32, 4, 16
        elif sample_bits == 8:
            item, itemsize, out_bits = np.uint8, 1, 8
        elif transport == "packed":
            item, itemsize, out_bits = np.uint16, 2, 12
        else:  # 16-bit raw
            item, itemsize, out_bits = np.uint16, 2, 16
        packed12 = (out_bits == 12)

        # Open the data socket once and reuse it across reads (the agent
        # lazy-accepts the one connection and reuses it). Each read() streams
        # exactly its records, so the socket stays in sync for the next call;
        # it's torn down in close() (or on any read error below).
        sock = self._ensure_data_socket()

        out = {ch: np.empty((n_out, out_len), dtype=item) for ch in chans}
        reader_err: list[BaseException] = []
        wire_bytes = [0]     # total bytes pulled over the data socket
        # Throttle the progress callback to ~100 ticks over the whole read: it
        # runs in the reader thread, so calling it per row would both add Python
        # overhead and backpressure the socket if the callback is slow.
        prog_step = max(1, n_out // 100)

        def _reader() -> None:
            # The agent emits, per row g, one record per channel in `chans`
            # order: record index = g * nch + ci → channel chans[ci], row g.
            try:
                for g in range(n_out):
                    for ch in chans:
                        n = struct.unpack("<I", _recv_exact(sock, 4))[0]
                        if n != out_len:
                            raise ReadbackShortRead(
                                f"record had {n} samples, expected {out_len}")
                        if packed12:
                            nbytes = ((n + 1) // 2) * 3
                            row = _unpack12(_recv_exact(sock, nbytes), n)
                        else:
                            nbytes = n * itemsize
                            row = np.frombuffer(_recv_exact(sock, nbytes),
                                                dtype=item, count=n)
                        wire_bytes[0] += 4 + nbytes
                        out[ch][g, :] = row
                    if progress is not None and ((g + 1) % prog_step == 0
                                                 or g + 1 == n_out):
                        progress(g + 1, n_out)
            except BaseException as e:   # noqa: BLE001 — surfaced after join
                reader_err.append(e)

        log.debug("read_frames: count=%d spf=%d channels=%s crop=(%d,%d) "
                  "average=%d wire=%s → %d×%d %s rows × %d ch",
                  count, full, chans, crop_lo, crop_hi, k,
                  ("i12-packed" if packed12 else item.__name__),
                  n_out, out_len, item.__name__, nch)
        t0 = time.monotonic()
        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        agent_exc: Exception | None = None
        status: dict = {}
        try:
            status = dict(self._script.exports_sync.read_frames({
                "first": int(first), "count": int(count),
                "samplesPerFrame": full, "channels": chans,
                "cropLo": int(crop_lo), "cropHi": int(crop_hi),
                "average": k, "outBits": out_bits,
                **({"timestamps": True} if timestamps else {}),
            }))
        except Exception as e:   # frida surfaces an agent throw here (RPCException)
            agent_exc = e

        # A failed/rejected request may stream fewer records than the reader
        # expects, leaving it blocked in recv. Detect failure and shut the socket
        # down BEFORE joining so recv returns at once — otherwise a synchronous
        # rejection (e.g. "channel not enabled") costs a full per-recv timeout.
        failed = (agent_exc is not None
                  or status.get("ret", 0) < 0 or status.get("err", 0) < 0)
        if failed:
            self._close_data_socket()
        t.join(timeout=60.0)

        if failed or reader_err:
            # On any failure the stream may be mid-record / desynced; the next
            # read() opens a fresh socket rather than reusing a corrupt one.
            self._close_data_socket()
            log.warning("read of %d frames failed after %d B; data socket reset",
                        count, wire_bytes[0])
            if agent_exc is not None:
                # Wrap the frida RPCException so the library surfaces a typed
                # error (e.g. requesting a not-enabled channel) rather than
                # leaking frida's exception to callers.
                raise AgentError(f"read_frames failed: {agent_exc}") from agent_exc
            if reader_err:
                raise ReadbackShortRead("stream read failed") from reader_err[0]
            raise AgentError(f"readFrames failed: {status}", status)

        dt = time.monotonic() - t0
        mbps = wire_bytes[0] / dt / 1e6 if dt > 0 else 0.0
        spec = f"avg={k}" + (f", crop {crop_lo}:{crop_hi}" if crop else "")
        log.info("read %d frames ×%d ch (%s) → %d×%d %s (%.1f MB/s, %.0f frame/s, "
                 "%.1f ms)", count, nch, spec, n_out, out_len, item.__name__,
                 mbps, count / dt if dt > 0 else 0.0, dt * 1e3)
        # Raw engine→agent DMA rate (the hardware ceiling, before deinterleave/
        # crop/encode and before the wire) — the absolute max the scope can read.
        dma_ms = status.get("dmaMs", 0)
        dma_bytes = status.get("dmaBytes", 0)
        if dma_ms > 0 and dma_bytes > 0:
            log.debug("DMA (engine read, pre-wire): %.1f MB in %.1f ms = %.1f MB/s "
                      "(%.0f frame/s)", dma_bytes / 1e6, dma_ms,
                      dma_bytes / dma_ms / 1e3, count / dma_ms * 1e3)
        if timestamps:
            try:
                self._last_frame_timestamps = FrameTimestamps.from_status(
                    status, first=int(first), count=int(count))
            except Exception as exc:
                self._close_data_socket()
                raise AgentError(f"invalid frame timestamps: {exc}") from exc
            # Avoid retaining a second, string-valued copy of the full vector.
            status.pop('frameTimestampTicks')
        self._last_read_stats = dict(status)
        return out

    def stream(self, *, samples_per_frame: int,
               channels: list[int] | tuple[int, ...] = (1,),
               crop: tuple[int, int] | None = None,
               sample_bits: int = 16, transport: str = "raw", batch: int = 0):
        """Yield frames from the agent-driven continuous stream, one per recorded
        waveform, until the caller stops iterating.

        Unlike read() (one host-armed batch), the agent owns the capture loop:
        it captures a batch itself (SetRun mode=2), waits for the FPGA to record
        it (getPlayInfo), replays + DMAs it, then sends it while the next batch
        captures. Yields ``{channel: ndarray}``, or a bare ndarray for a single
        channel. Raw encodings only (`sample_bits` 16/8, `transport` raw/packed);
        no averaging. `batch` caps frames per capture (<=0 → the hardware
        dwMaxFrameCount); the agent sizes each capture below that to the trigger
        rate. Breaking out of the loop stops the agent and closes the socket."""
        import numpy as np

        if sample_bits not in (16, 8):
            raise ValueError(f"sample_bits must be 16 or 8, got {sample_bits}")
        if transport not in ("raw", "packed"):
            raise ValueError(f"transport must be 'raw' or 'packed', got {transport!r}")
        if sample_bits == 8 and transport == "packed":
            raise ValueError("transport='packed' applies only to sample_bits=16")
        if self._script is None:
            raise ScopeNotFound("not open()ed")

        chans = [int(c) for c in channels]
        if not chans:
            raise ValueError("no channels to stream")
        full = int(samples_per_frame)
        if crop is not None:
            crop_lo, crop_hi = int(crop[0]), int(crop[1])
            if not (0 <= crop_lo < crop_hi <= full):
                raise ValueError(
                    f"crop {(crop_lo, crop_hi)} out of range for {full} samples/frame")
            out_len = crop_hi - crop_lo
        else:
            crop_lo = crop_hi = 0
            out_len = full

        if sample_bits == 8:
            item, itemsize, out_bits = np.uint8, 1, 8
        elif transport == "packed":
            item, itemsize, out_bits = np.uint16, 2, 12
        else:
            item, itemsize, out_bits = np.uint16, 2, 16
        packed12 = (out_bits == 12)
        single = len(chans) == 1

        sock = self._ensure_data_socket()
        log.info("stream: channels=%s spf=%d crop=(%d,%d) wire=%s batch=%d",
                 chans, full, crop_lo, crop_hi,
                 ("i12-packed" if packed12 else item.__name__), batch)
        started = False
        try:
            # A just-stopped stream may still be winding down (the agent clears
            # its `streaming` flag after the loop's finally restores export), so
            # retry briefly if it reports "already running".
            r: dict = {}
            for _ in range(20):
                r = dict(self._script.exports_sync.stream_frames({
                    "samplesPerFrame": full, "channels": chans,
                    "cropLo": crop_lo, "cropHi": crop_hi,
                    "outBits": out_bits, "batch": int(batch),
                }))
                if r.get("ok") or "already running" not in str(r.get("error", "")):
                    break
                time.sleep(0.05)
            if not r.get("ok"):
                raise AgentError(f"stream_frames failed: {r}")
            started = True
            while True:
                frame = {}
                for ch in chans:
                    n = struct.unpack("<I", _recv_exact(sock, 4))[0]
                    if n != out_len:
                        raise ReadbackShortRead(
                            f"record had {n} samples, expected {out_len}")
                    if packed12:
                        row = _unpack12(_recv_exact(sock, ((n + 1) // 2) * 3), n)
                    else:
                        row = np.frombuffer(_recv_exact(sock, n * itemsize),
                                            dtype=item, count=n)
                    frame[ch] = row
                yield frame[chans[0]] if single else frame
        finally:
            # Order matters. The agent may be blocked in the C send with the
            # socket buffer full (a large batch we stopped reading mid-way);
            # RPCs run on the same JS thread, so stream_stop would never be
            # serviced and we'd deadlock waiting for its reply. Shut the socket
            # down first: the send fails, the loop breaks and restores export.
            # Then stream_stop covers the other case (loop polling for a
            # capture, not writing), and the fds are dropped.
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
            if started:
                try:
                    self._script.exports_sync.stream_stop()
                except Exception:
                    pass
            self._close_data_socket()

    def _ensure_data_socket(self) -> "socket.socket":
        """Open the agent's listen socket + connect, once; reuse thereafter."""
        if self._sock is not None:
            return self._sock
        port = self._data_port
        r = dict(self._script.exports_sync.readback_open(port))
        if not r.get("ok"):
            raise AgentError(f"readback_open failed: {r}")
        sock = socket.create_connection((self._host, port), timeout=10.0)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Per-recv stall watchdog: a wedged DMA surfaces as a timeout (→
        # ReadbackShortRead) instead of hanging the reader thread forever.
        sock.settimeout(20.0)
        self._sock = sock
        log.debug("data socket open %s:%d", self._host, port)
        return sock

    def _close_data_socket(self) -> None:
        """Tear down the data socket + the agent's listen/client fds. Shutdown
        first so a write-stalled agent unblocks and readback_close can return."""
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        try:
            if self._script is not None:
                self._script.exports_sync.readback_close()
        except Exception:
            pass
        log.debug("data socket closed")

    def close(self) -> None:
        self._last_frame_timestamps = None
        self._last_read_stats = None
        self._close_data_socket()   # shutdown first → unblocks a stalled agent
        try:
            if self._script is not None:
                self._script.exports_sync.dispose()
        except Exception:
            pass
        for obj in (self._script, self._session):
            try:
                if obj is not None:
                    obj.unload() if obj is self._script else obj.detach()
            except Exception:
                pass
        self._script = self._session = self._device = None

    def __enter__(self) -> "Readback":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
