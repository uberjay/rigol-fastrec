"""frida_server: the ensure() decision table, download/cache, and the
WaveRecorder opt-in, all without adb, a scope, or the network."""

import lzma
from pathlib import Path
from types import SimpleNamespace

import pytest

from rigol_fastrec import WaveRecorder
from rigol_fastrec import frida_server as fs
from rigol_fastrec.exceptions import FridaServerError

WANT = "17.9.11"


class FakeAdb:
    """Stands in for fs.Adb: records every call, serves --version from
    `installed`, and 'installs' whatever is pushed as `push_version`."""
    instances: list = []

    def __init__(self, serial, *, executable="adb", timeout=30.0):
        self.serial, self.calls = serial, []
        self.installed = FakeAdb.installed
        FakeAdb.instances.append(self)

    installed = None          # class-level defaults, set per test
    push_version = WANT

    def connect(self):
        self.calls.append(("connect",))

    def shell(self, command, *, check=False, timeout=None):
        self.calls.append(("shell", command))
        if "--version" in command:
            return (self.installed + "\n") if self.installed else "sh: not found\n"
        return ""

    def push(self, local, remote):
        self.calls.append(("push", str(local), remote))
        self.installed = FakeAdb.push_version


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake adb + host version + no sleeping; `reach` is the scripted answer
    sequence for server_reachable()."""
    FakeAdb.instances = []
    FakeAdb.installed = None
    FakeAdb.push_version = WANT
    monkeypatch.setattr(fs, "Adb", FakeAdb)
    monkeypatch.setattr(fs, "host_version", lambda: WANT)
    monkeypatch.setattr(fs, "_sleep", lambda s: None)
    monkeypatch.setattr(fs, "_wait_gone", lambda *a, **k: None)
    state = SimpleNamespace(reach=[], binary=tmp_path / "frida-server-bin")
    state.binary.write_bytes(b"ELF")

    def reachable(host, *, frida_port=fs.FRIDA_PORT):
        return state.reach.pop(0) if len(state.reach) > 1 else state.reach[0]
    monkeypatch.setattr(fs, "server_reachable", reachable)
    return state


def shells(adb):
    return [c[1] for c in adb.calls if c[0] == "shell"]


def test_running_server_needs_no_adb(env):
    env.reach = [True]
    r = fs.ensure("scope", install=False)
    assert r.action == "running" and r.version is None    # adb not consulted
    assert str(r) == "frida-server already running on scope"
    assert FakeAdb.instances == []


def test_starts_installed_matching_server(env):
    env.reach = [False, False, True]         # down, poll miss, then up
    FakeAdb.installed = WANT
    r = fs.ensure("scope", install=False)
    assert r.action == "started"
    (adb,) = FakeAdb.instances
    assert adb.serial == "scope:55555"
    start = [s for s in shells(adb) if "-l 0.0.0.0:27042 -D" in s]
    assert len(start) == 1 and start[0].startswith("su -c '/data/local/tmp/frida-server")
    assert not any(c[0] == "push" for c in adb.calls)


def test_missing_binary_without_install_names_the_command(env):
    env.reach = [False]
    with pytest.raises(FridaServerError) as ei:
        fs.ensure("scope", install=False)
    msg = str(ei.value)
    assert "nothing runnable at /data/local/tmp/frida-server" in msg
    assert "python -m rigol_fastrec.frida_server --host scope" in msg
    assert not any(c[0] == "push" for c in FakeAdb.instances[0].calls)


def test_incompatible_binary_without_install_refuses(env):
    env.reach = [False]
    FakeAdb.installed = "17.8.2"
    with pytest.raises(FridaServerError, match="found 17.8.2"):
        fs.ensure("scope", install=False)


def test_patch_level_difference_is_compatible(env):
    env.reach = [False, True]
    FakeAdb.installed = "17.9.6"             # host is 17.9.11
    r = fs.ensure("scope", install=False)
    assert r.action == "started" and r.version == "17.9.6"
    assert not any(c[0] == "push" for c in FakeAdb.instances[0].calls)


def test_compatible_rule():
    assert fs.compatible("17.9.6", "17.9.11")
    assert not fs.compatible("17.8.9", "17.9.11")
    assert not fs.compatible("16.9.11", "17.9.11")
    assert not fs.compatible(None, "17.9.11")


def test_install_pushes_binary_then_starts(env):
    env.reach = [False, True]
    r = fs.ensure("scope", install=True, server_binary=env.binary)
    assert r.action == "installed"
    (adb,) = FakeAdb.instances
    kinds = [c[0] for c in adb.calls]
    assert kinds.index("push") < len(kinds) - 1
    assert ("push", str(env.binary), fs.REMOTE_PATH) in adb.calls
    sh = shells(adb)
    assert any(s.startswith("chmod 755 ") for s in sh)
    assert sh.index(next(s for s in sh if s.startswith("chmod"))) < \
        sh.index(next(s for s in sh if "-D" in s))


def test_install_replaces_running_incompatible_server(env):
    env.reach = [True, True]                 # up (old), then up again after start
    FakeAdb.installed = "17.8.2"
    r = fs.ensure("scope", install=True, server_binary=env.binary)
    assert r.action == "installed"
    sh = shells(FakeAdb.instances[0])
    assert any("killall frida-server" in s for s in sh)
    assert sh.index(next(s for s in sh if "killall" in s)) < \
        sh.index(next(s for s in sh if "-D" in s))


def test_install_with_matching_running_server_is_a_noop(env):
    env.reach = [True]
    FakeAdb.installed = WANT
    r = fs.ensure("scope", install=True)
    assert r.action == "running"
    assert not any("-D" in s for s in shells(FakeAdb.instances[0]))


def test_restart_stops_and_starts(env):
    env.reach = [True, True]
    FakeAdb.installed = WANT
    r = fs.ensure("scope", install=True, restart=True)
    assert r.action == "started"
    sh = shells(FakeAdb.instances[0])
    assert any("killall" in s for s in sh) and any("-D" in s for s in sh)


def test_pushed_binary_of_wrong_version_is_an_error(env):
    env.reach = [False]
    FakeAdb.push_version = "17.8.2"
    with pytest.raises(FridaServerError, match="reports version 17.8.2"):
        fs.ensure("scope", install=True, server_binary=env.binary)


def test_start_that_never_answers_times_out(env, monkeypatch):
    env.reach = [False]
    FakeAdb.installed = WANT
    clock = iter([0.0, 0.0, 1.0, 100.0, 100.0])
    monkeypatch.setattr(fs.time, "monotonic", lambda: next(clock, 100.0))
    with pytest.raises(FridaServerError, match="did not answer"):
        fs.ensure("scope", install=False, start_timeout=5.0)


def test_parse_version():
    assert fs.parse_version("17.9.11\n") == "17.9.11"
    assert fs.parse_version("  17.9.11  ") == "17.9.11"
    assert fs.parse_version("sh: /data/local/tmp/frida-server: not found\n") is None
    assert fs.parse_version("") is None
    assert fs.parse_version("/data/local/tmp/frida-server: Permission denied") is None


def test_fetch_server_uses_cache_and_decompresses(tmp_path, monkeypatch):
    cached = tmp_path / f"frida-server-{WANT}-android-arm64"
    cached.write_bytes(b"cached")
    monkeypatch.setattr(fs.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert fs.fetch_server(WANT, cache_dir=tmp_path) == cached

    class Resp:
        def __init__(self, data): self.data, self.pos = data, 0
        def read(self, n=-1):
            chunk = self.data[self.pos:] if n < 0 else self.data[self.pos:self.pos + n]
            self.pos += len(chunk)
            return chunk
        def __enter__(self): return self
        def __exit__(self, *a): return False
    seen = {}

    def urlopen(url, timeout=None):
        seen["url"] = url
        return Resp(lzma.compress(b"ELF-binary"))
    monkeypatch.setattr(fs.urllib.request, "urlopen", urlopen)
    out = fs.fetch_server("1.2.3", cache_dir=tmp_path)
    assert out.read_bytes() == b"ELF-binary"
    assert seen["url"] == fs.RELEASE_URL.format(version="1.2.3")
    assert not list(tmp_path.glob("*.part"))


def test_fetch_server_download_failure_is_typed(tmp_path, monkeypatch):
    def urlopen(url, timeout=None):
        raise OSError("no route")
    monkeypatch.setattr(fs.urllib.request, "urlopen", urlopen)
    with pytest.raises(FridaServerError, match="could not download"):
        fs.fetch_server("9.9.9", cache_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_cli_reports_and_maps_errors(monkeypatch, capsys):
    monkeypatch.setattr(fs, "ensure",
                        lambda host, **kw: fs.EnsureResult(host, WANT, "installed"))
    assert fs.main(["--host", "scope"]) == 0
    assert "installed and started on scope" in capsys.readouterr().out

    def boom(host, **kw):
        raise FridaServerError("nope")
    monkeypatch.setattr(fs, "ensure", boom)
    assert fs.main(["--host", "scope"]) == 1
    assert "error: nope" in capsys.readouterr().err


def test_cli_passes_install_and_options(monkeypatch):
    seen = {}

    def fake(host, **kw):
        seen.update(kw, host=host)
        return fs.EnsureResult(host, WANT, "started")
    monkeypatch.setattr(fs, "ensure", fake)
    assert fs.main(["--host", "h", "--restart", "--adb-port", "5556",
                    "--server-binary", "/tmp/x"]) == 0
    assert seen["install"] is True and seen["restart"] is True
    assert seen["adb_port"] == 5556 and seen["server_binary"] == "/tmp/x"


def test_waverecorder_flag_is_off_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(fs, "ensure", lambda *a, **k: calls.append((a, k)))
    rec = WaveRecorder("scope")
    rec._scpi = SimpleNamespace(open=lambda: None, model="MHO98", firmware="00.01.00")
    rec._rb = SimpleNamespace(open=lambda **kw: None)
    rec.__enter__()
    assert calls == []


def test_waverecorder_flag_starts_but_never_installs(monkeypatch):
    calls = []

    def fake(host, **kw):
        calls.append((host, kw))
        return fs.EnsureResult(host, WANT, "started")
    monkeypatch.setattr(fs, "ensure", fake)
    order = []
    rec = WaveRecorder("scope", ensure_frida_server=True, frida_port=27043,
                       adb="/opt/adb", adb_port=5556)
    rec._scpi = SimpleNamespace(open=lambda: order.append("scpi"),
                                model="MHO98", firmware="00.01.00")
    rec._rb = SimpleNamespace(open=lambda **kw: order.append("frida"))
    rec.__enter__()
    (host, kw), = calls
    assert host == "scope" and kw["install"] is False
    assert kw["frida_port"] == 27043 and kw["adb"] == "/opt/adb" and kw["adb_port"] == 5556
    assert order == ["scpi", "frida"]      # ensure runs after the *IDN? check
