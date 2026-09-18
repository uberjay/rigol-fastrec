# rigol-fastrec

Use a **Rigol MHO900-series** oscilloscope as a fast streaming ADC. It drives the
scope's WaveRecord ("fast record", segmented 1M-wfm/s) memory and pulls frames
back through a Frida-injected agent, with averaging, channel deinterleave, and
sample cropping all done on the scope.

## Status

Validated on an MHO98 (firmware `00.01.00`):

- WaveRecord readback over LAN, about 11.7 MB/s on the built-in Ethernet
  (`Readback`).
- The SCPI control plane plus the `WaveRecorder` facade: `configure → run →
  wait_recorded → read`, with frame-averaging, multi-channel deinterleave, and
  crop all on the scope. Multiple channels come out of a single DMA pass as
  `{channel: ndarray}`.
- Continuous streaming (`stream()`): the agent runs the capture loop itself and
  yields one frame per trigger until you stop, for live viewing or open-ended
  capture.
- Python 3.12+.

Probably adaptable to other Rigol scopes running Android.

## Install

```bash
git clone https://github.com/uberjay/rigol-fastrec && cd rigol-fastrec
python -m venv .venv && source .venv/bin/activate
pip install -e .           # installs rigol_fastrec + numpy / pyvisa / pyvisa-py / frida
```

The built Frida agent (`rigol_fastrec/_agent.js`) is committed and ships as
package data, so the install needs no Node toolchain. (Only rebuild it if you
change `agent/`; see Toolchain.)

## Set up frida-server on the scope

The MHO900 runs Android, so you need a frida-server running on it, reached over
`adb` (Android Debug Bridge). Push a build matching the host `frida` version
(17.9.x) for `android-arm64`:

```bash
curl -L -O https://github.com/frida/frida/releases/download/17.9.6/frida-server-17.9.6-android-arm64.xz
unxz frida-server-17.9.6-android-arm64.xz

adb connect 192.168.1.99:55555 # replace with your scope's IP
adb push frida-server-17.9.6-android-arm64 /data/local/tmp/frida-server
adb shell "chmod 755 /data/local/tmp/frida-server"
```

Then start it as root, listening on the LAN. Re-run this after every scope
reboot; nothing auto-starts it from `/data/local/tmp`:

```bash
adb shell "su -c '/data/local/tmp/frida-server -l 0.0.0.0:27042 -D &'"
```

## Quickstart

```python
from rigol_fastrec import WaveRecorder, Trigger, Channel

with WaveRecorder(host="10.0.80.80") as rec:
    rec.configure(samples=1000, sample_rate=1e9,
                  trigger=Trigger(source="CHAN2", level=1.5),
                  channels={1: Channel(range=0.5)})

    # 1) record 64 frames, read channel 1 as raw 16-bit codes → volts
    rec.run(64);

    # perform application-specific trigger routine here

    rec.wait_recorded()
    frames = rec.read(count=64, channel=1)              # uint16 (64, 1000)
    volts = rec.to_volts(frames, channel=1)

    # 2) in-agent averaging: mean of every 10 frames → float32, less noise
    rec.run(640); rec.wait_recorded()
    averaged_frames = rec.read(count=640, channel=1, average=10)   # float32 (64, 1000)

    # 3) trade precision for wire speed (see docs/API.md)
    rec.run(64); rec.wait_recorded()
    eightbit_frames = rec.read(count=64, channel=1, sample_bits=8)        # uint8,  ~2x faster
    frames = rec.read(count=64, channel=1, transport="packed")  # uint16, ~1.33x faster

    # 4) continuous: the agent captures and yields one frame per trigger until you break
    rec.scpi.write(":TRIGger:SWEep AUTO")               # free-run; omit to wait for real triggers
    for frame in rec.stream(channel=1):                 # uint16 (1000,)
        ...
        break
```

`configure()` once, then `run → wait_recorded → read` per batch -- fire your DUT
triggers between `run()` and `wait_recorded()` (the examples self-trigger, so
they need no DUT). `stream()` replaces that loop for open-ended capture. See
`examples/` for runnable scripts and [docs/API.md](docs/API.md) for the full
`WaveRecorder` reference: every method and read/configure option, with the
speed/precision trade-offs.

## Running the examples

The examples live in `examples/` and import the installed package, so run them
straight from the checkout.

Capture (scope reachable at `--host`; SCPI on :5555, frida-server on :27042).
`capture_basic` and `throughput_bench` self-trigger (AUTO sweep), so they run
with no DUT attached:

```bash
# record N frames on one channel, save to .npy
python examples/capture_basic.py --host 10.0.80.80 --frames 64 --samples 1000

# readback throughput across block sizes, raw vs averaged
python examples/throughput_bench.py --host 10.0.80.80 \
    --batches 64,256,1024 --average 1,8
```

`stream_viewer` is a live plot over `stream()` (latest frame plus a rolling
average; drag to read off a `--crop` window). It needs the `viewer` extra
(pyqtgraph + PyQt6):

```bash
pip install -e '.[viewer]'
python examples/stream_viewer.py --host 10.0.80.80 --channel 1 --range 1.0
python examples/stream_viewer.py --host 10.0.80.80 --channel 1 --no-auto   # real triggers only
```

## Tests

```bash
pip install -e '.[dev]'
pytest                       # offline; no scope/Frida needed
```

`make check` also runs them, plus type-checks the agent (that half needs Node).

## Layout

```
agent/    TypeScript Frida agent (frida-compile); builds the committed bundle below
python/   rigol_fastrec -- host library + committed _agent.js bundle, and its tests
          (pyproject.toml is at the repo root)
examples/ runnable scripts: capture_basic, throughput_bench, stream_viewer
docs/     API.md -- WaveRecorder reference; VALIDATION.md -- on-scope tests;
          DESIGN.md -- architecture & design notes
```

## Safety / scope

Targets the MHO900 series only:

1. The host checks `*IDN?` (model, firmware version) against a supported set
   before attaching Frida.
2. The agent re-checks that pair against the `firmware.ts` whitelist as the
   first thing `resolve()` does, then resolves symbols. An unrecognized build,
   or a missing symbol, aborts with `UnsupportedFirmware`.

Every native offset is firmware-specific, so applying them to an unprofiled
build could wedge the scope. That's why the check fails closed.

The supported set today is just `MHO98 / 00.01.00` -- a different MHO900 firmware
will refuse until you add a profile to `firmware.ts` (see
[docs/DESIGN.md](docs/DESIGN.md)).

## Security

- The stock Rigol software exposes `adb` over the network with no authentication.
- frida-server (`:27042`) and the data socket (`:5028`) are unauthenticated and
  unencrypted.

Do with that what you will.

## Toolchain

- Run: Python 3.12+; `frida` 17.x, `pyvisa` + `pyvisa-py`, `numpy` (all pulled
  in by `pip install`). No Node.
- Rebuild the agent (only if you change `agent/`): Node 20+, `frida-compile`,
  `@types/frida-gum`, TypeScript 5.x. Run `make build` (or `cd agent && npm
  install && npm run build`), which writes `python/rigol_fastrec/_agent.js`
  directly.

## Increasing readback performance

The engine→agent DMA on the scope runs at about 470 MB/s (~117k frames/s at 1000
samples), far faster than any readback link, so the wire is always the
bottleneck. To go faster, send less data: cropping and averaging on the scope
help a lot.

Bigger socket buffers help a little:

```
adb shell "su -c 'sysctl -w net.core.wmem_max=16777216'"
adb shell "su -c 'sysctl -w net.ipv4.tcp_wmem=\"4096 262144 16777216\"'"
```

The built-in port is 100 MbE, which caps raw readback at ~11.7 MB/s. For more, a
USB Gigabit adapter based on the Realtek RTL8153 (the `r8152` driver) in the
scope's front-panel USB port gets you to about 27 MB/s. It comes up as a second
network interface; bring it up on the scope and point `--host` at its address.

The Android routing and firewall setup needs a routing rule:

```
adb shell "su -c 'ip a add 10.0.80.80/24 dev eth1'"
adb shell "su -c 'ip link set up dev eth1'"
adb shell "su -c 'ip rule add to 10.0.80.0/24 lookup main pref 18000'"
```

## LLM Use Disclosure

Opus 4.8 and Fable 5 were used in the development of this software. Extensive
hand-validation on actual hardware (an MHO98) has been performed. All of the code,
regardless of who wrote it, has been reviewed for correctness to the best of
my ability. It is, however, software and therefore may have bugs. Issues and PRs
are 100% encouraged and welcome.

## License

MIT. See [LICENSE](LICENSE).
