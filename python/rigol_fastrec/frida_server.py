"""Provision and start frida-server on the scope over ADB (opt-in).

Automates the two manual steps in the README: pushing a frida-server build
that matches the host's ``frida`` package, and starting it after every scope
reboot. Everything runs from the host over ADB (the scope exposes ``adb`` on
:55555 with no authentication), and nothing on the scope changes persistently:
the binary sits in ``/data/local/tmp`` and the server is started per boot.
The scope's init scripts are never touched.

Two entry points, deliberately split:

* ``python -m rigol_fastrec.frida_server --host HOST`` does everything:
  downloads the release matching the host ``frida`` version (or takes
  ``--server-binary``), pushes it, and starts it. Run it once, and again
  after upgrading ``frida`` on the host.
* ``WaveRecorder(host, ensure_frida_server=True)`` only STARTS a server that
  is already installed with the right version. It never downloads or
  replaces the binary. A missing or mismatched one raises
  ``FridaServerError`` naming the command above.

Requires ``adb`` on the host PATH only when it has to act; a server that is
already answering costs one TCP connection to detect.
"""

from __future__ import annotations

import argparse
import logging
import lzma
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .exceptions import FridaServerError

log = logging.getLogger("rigol_fastrec.frida_server")

REMOTE_PATH = "/data/local/tmp/frida-server"
ADB_PORT = 55555          # the MHO900's adbd listens here, not the usual 5555
FRIDA_PORT = 27042
RELEASE_URL = ("https://github.com/frida/frida/releases/download/"
               "{version}/frida-server-{version}-android-arm64.xz")
INSTALL_HINT = "python -m rigol_fastrec.frida_server --host {host}"

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_sleep = time.sleep


def host_version() -> str:
    """The host ``frida`` package version; the server on the scope must match."""
    import frida
    return frida.__version__


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "rigol-fastrec"


def parse_version(text: str) -> str | None:
    """``frida-server --version`` output → 'x.y.z', or None for anything else
    (missing binary, 'not found', 'Permission denied', empty)."""
    for line in text.splitlines():
        m = _VERSION_RE.fullmatch(line.strip())
        if m:
            return m.group(1)
    return None


def compatible(have: str | None, want: str) -> bool:
    """Server/host compatibility is judged on major.minor (frida's wire protocol
    holds across patch releases; the README's guidance is '17.9.x')."""
    if not have:
        return False
    return have.split(".")[:2] == want.split(".")[:2]


# --- reachability ----------------------------------------------------------

def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _frida_answers(host: str, port: int) -> bool:
    import frida
    try:
        dev = frida.get_device_manager().add_remote_device(f"{host}:{port}")
        dev.enumerate_processes()
        return True
    except Exception as e:   # frida errors share no base class
        log.debug("frida-server at %s:%d not answering: %s", host, port, e)
        return False


def server_reachable(host: str, *, frida_port: int = FRIDA_PORT) -> bool:
    """True if a frida-server answers on the port. A bare TCP probe first, so
    an unreachable host fails in seconds instead of frida's connect timeout."""
    return _port_open(host, frida_port) and _frida_answers(host, frida_port)


def _wait_reachable(host: str, frida_port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if server_reachable(host, frida_port=frida_port):
            return True
        if time.monotonic() >= deadline:
            return False
        _sleep(0.5)


def _wait_gone(host: str, frida_port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while _port_open(host, frida_port) and time.monotonic() < deadline:
        _sleep(0.2)


# --- adb ---------------------------------------------------------------------

class Adb:
    """Thin ``adb`` CLI wrapper bound to one device serial (``host:port``)."""

    def __init__(self, serial: str, *, executable: str = "adb",
                 timeout: float = 30.0) -> None:
        exe = shutil.which(executable)
        if exe is None:
            raise FridaServerError(
                f"adb executable not found: {executable!r} (install Android "
                "platform-tools or pass adb=/path/to/adb)")
        self.executable, self.serial, self.timeout = exe, serial, timeout

    def _run(self, args: list[str], *, with_serial: bool = True,
             timeout: float | None = None, check: bool = True) -> str:
        cmd = [self.executable] + (["-s", self.serial] if with_serial else []) + args
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired as e:
            raise FridaServerError(f"adb timed out: {' '.join(cmd)}") from e
        if check and r.returncode != 0:
            detail = (r.stderr or r.stdout).strip()
            raise FridaServerError(
                f"adb failed ({r.returncode}): {' '.join(cmd)}: {detail}")
        return r.stdout

    def connect(self) -> None:
        out = self._run(["connect", self.serial], with_serial=False, check=False)
        if "connected to" not in out:     # "connected to X" / "already connected to X"
            raise FridaServerError(f"adb connect {self.serial}: {out.strip() or 'no response'}")
        state = self._run(["get-state"], check=False).strip()
        if state != "device":
            raise FridaServerError(f"adb device {self.serial} not ready: {state!r}")

    def shell(self, command: str, *, check: bool = False,
              timeout: float | None = None) -> str:
        return self._run(["shell", command], check=check, timeout=timeout)

    def push(self, local: Path, remote: str) -> None:
        self._run(["push", str(local), remote], timeout=180)


# --- the pieces ---------------------------------------------------------------

def installed_version(adb: Adb, remote_path: str = REMOTE_PATH) -> str | None:
    """Version of the frida-server binary on the scope, or None if absent or
    not runnable."""
    return parse_version(adb.shell(f"{remote_path} --version 2>/dev/null"))


def fetch_server(version: str, *, cache_dir: Path | str | None = None,
                 url: str | None = None) -> Path:
    """Download + decompress ``frida-server-<version>-android-arm64.xz`` into
    the cache (or return the cached copy). Returns the decompressed path."""
    cache = Path(cache_dir) if cache_dir else default_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    dst = cache / f"frida-server-{version}-android-arm64"
    if dst.is_file() and dst.stat().st_size > 0:
        return dst
    src = url or RELEASE_URL.format(version=version)
    log.info("downloading %s", src)
    tmp = dst.with_name(dst.name + ".part")
    try:
        with urllib.request.urlopen(src, timeout=60) as resp, \
                lzma.open(resp) as xz, tmp.open("wb") as out:
            shutil.copyfileobj(xz, out)
    except (OSError, lzma.LZMAError, EOFError) as e:   # URLError is an OSError
        tmp.unlink(missing_ok=True)
        raise FridaServerError(
            f"could not download frida-server {version} from {src}: {e}") from e
    tmp.replace(dst)
    return dst


def install_server(adb: Adb, local: Path, *, remote_path: str = REMOTE_PATH) -> None:
    adb.push(local, remote_path)
    adb.shell(f"chmod 755 {remote_path}", check=True)


def start_server(adb: Adb, *, remote_path: str = REMOTE_PATH,
                 frida_port: int = FRIDA_PORT) -> None:
    """Start the server as root, daemonized, listening on the LAN. Same line
    as the README, with stdio detached so ``adb shell`` returns."""
    adb.shell(f"su -c '{remote_path} -l 0.0.0.0:{frida_port} -D "
              f">/dev/null 2>&1 </dev/null &'", check=True, timeout=15)


def stop_server(adb: Adb) -> None:
    adb.shell("su -c 'killall frida-server'")      # no-op if none running


@dataclass(frozen=True)
class EnsureResult:
    host: str
    version: str | None   # the server's version; None when it was found
                          # answering and adb was not consulted
    action: str           # "running" | "started" | "installed"

    def __str__(self) -> str:
        what = {"running": "already running", "started": "started",
                "installed": "installed and started"}[self.action]
        ver = f" {self.version}" if self.version else ""
        return f"frida-server{ver} {what} on {self.host}"


def ensure(host: str, *, install: bool = False, restart: bool = False,
           frida_port: int = FRIDA_PORT, adb_port: int = ADB_PORT,
           adb: str = "adb", adb_serial: str | None = None,
           remote_path: str = REMOTE_PATH, version: str | None = None,
           server_binary: Path | str | None = None,
           cache_dir: Path | str | None = None,
           start_timeout: float = 20.0) -> EnsureResult:
    """Make sure a matching frida-server answers on ``host:frida_port``.

    ``install=False`` (the ``WaveRecorder`` flag): if a server answers, return
    at once; otherwise start the installed binary. Never downloads or replaces
    it: a missing or mismatched binary raises ``FridaServerError``.

    ``install=True`` (the CLI): also verify the installed version against the
    host ``frida`` package, and download (or push ``server_binary``) and
    restart when it is incompatible. ``restart=True`` restarts even a
    matching one. Compatibility is major.minor (see ``compatible``).
    """
    want = version or host_version()
    up = server_reachable(host, frida_port=frida_port)
    if up and not install and not restart:
        return EnsureResult(host, None, "running")

    bridge = Adb(adb_serial or f"{host}:{adb_port}", executable=adb)
    bridge.connect()
    have = installed_version(bridge, remote_path)
    action = "started"
    if not compatible(have, want):
        if not install:
            found = f"found {have}" if have else f"nothing runnable at {remote_path}"
            raise FridaServerError(
                f"no frida-server compatible with host frida {want} is installed "
                f"on {host} ({found}); run: {INSTALL_HINT.format(host=host)}")
        local = Path(server_binary) if server_binary else \
            fetch_server(want, cache_dir=cache_dir)
        if not local.is_file():
            raise FridaServerError(f"server binary not found: {local}")
        if up:                       # can't overwrite a running executable
            stop_server(bridge)
            _wait_gone(host, frida_port, 5.0)
            up = False
        log.info("pushing %s to %s:%s", local.name, host, remote_path)
        install_server(bridge, local, remote_path=remote_path)
        got = installed_version(bridge, remote_path)
        if not compatible(got, want):
            raise FridaServerError(
                f"pushed binary reports version {got or 'unknown'}, expected {want}")
        have, action = got, "installed"
    elif up:
        if not restart:
            return EnsureResult(host, have, "running")
        stop_server(bridge)
        _wait_gone(host, frida_port, 5.0)

    log.info("starting frida-server %s on %s:%d", have, host, frida_port)
    start_server(bridge, remote_path=remote_path, frida_port=frida_port)
    if not _wait_reachable(host, frida_port, start_timeout):
        raise FridaServerError(
            f"frida-server did not answer on {host}:{frida_port} within "
            f"{start_timeout:g} s after starting (is su available on the scope?)")
    return EnsureResult(host, have, action)


def status(host: str, *, frida_port: int = FRIDA_PORT, adb_port: int = ADB_PORT,
           adb: str = "adb", adb_serial: str | None = None,
           remote_path: str = REMOTE_PATH) -> dict:
    """Report without changing anything: host version, whether a server
    answers, and the installed binary's version (None if adb can't reach)."""
    out = {"host": host, "want": host_version(),
           "reachable": server_reachable(host, frida_port=frida_port),
           "installed": None, "adb_error": None}
    try:
        bridge = Adb(adb_serial or f"{host}:{adb_port}", executable=adb)
        bridge.connect()
        out["installed"] = installed_version(bridge, remote_path)
    except FridaServerError as e:
        out["adb_error"] = str(e)
    return out


# --- CLI -----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m rigol_fastrec.frida_server",
        description="Install (if needed) and start frida-server on the scope over "
                    "ADB, matching the host frida package version. Nothing on the "
                    "scope is changed persistently.")
    p.add_argument("--host", required=True, help="scope IP / hostname")
    p.add_argument("--adb", default="adb", help="adb executable (default: adb on PATH)")
    p.add_argument("--adb-port", type=int, default=ADB_PORT)
    p.add_argument("--adb-serial", default=None,
                   help="adb serial to use instead of HOST:ADB_PORT")
    p.add_argument("--frida-port", type=int, default=FRIDA_PORT)
    p.add_argument("--remote-path", default=REMOTE_PATH)
    p.add_argument("--version", default=None,
                   help="frida-server version to install (default: the host frida package's)")
    p.add_argument("--server-binary", default=None,
                   help="decompressed frida-server-<ver>-android-arm64 to push instead of downloading")
    p.add_argument("--cache-dir", default=None,
                   help=f"download cache (default: {default_cache_dir()})")
    p.add_argument("--restart", action="store_true",
                   help="stop a running server and start it again")
    p.add_argument("--status", action="store_true", help="report only; change nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")
    common = dict(frida_port=args.frida_port, adb_port=args.adb_port, adb=args.adb,
                  adb_serial=args.adb_serial, remote_path=args.remote_path)
    try:
        if args.status:
            s = status(args.host, **common)
            print(f"host frida: {s['want']}")
            print(f"server answering on {args.host}:{args.frida_port}: "
                  f"{'yes' if s['reachable'] else 'no'}")
            if s["adb_error"]:
                print(f"installed on scope: unknown ({s['adb_error']})")
            else:
                print(f"installed on scope: {s['installed'] or 'none'}")
            return 0
        r = ensure(args.host, install=True, restart=args.restart, version=args.version,
                   server_binary=args.server_binary, cache_dir=args.cache_dir, **common)
    except FridaServerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
