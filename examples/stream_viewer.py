"""Live waveform viewer over rigol-fastrec streaming.

Opens a continuous, agent-driven stream from the scope and shows the most
recent frame (yellow) plus a rolling average (cyan), redrawing at ~30 Hz.
Drag-select a sample range to read off start/end indices (handy for dialing in
a --crop window for capture).

This uses WaveRecorder.stream(), where the agent owns the capture loop and keeps
the wire full. By default it self-triggers (AUTO sweep), so frames flow with no
DUT -- just feed a signal (e.g. the built-in AFG). Pass --no-auto to capture only
on real triggers (NORM sweep), e.g. a DUT firing in a loop.

Needs the viewer extra (pyqtgraph + PyQt6):  pip install -e '.[viewer]'

    python stream_viewer.py --host 10.0.80.80 --channel 1 --range 1.0
    python stream_viewer.py --host 10.0.80.80 --channel 1 --samples 2000 --crop 500,1500
    python stream_viewer.py --host 10.0.80.80 --channel 1 --sample-bits 8   # 2x faster wire
    python stream_viewer.py --host 10.0.80.80 --channel 1 --no-auto         # real triggers
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from collections import deque

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtWidgets

from rigol_fastrec import WaveRecorder, Trigger, Channel


class StreamViewer(QtWidgets.QMainWindow):
    def __init__(self, rec, *, channel, sample_bits, transport, crop, batch,
                 avg_window, avg_only):
        super().__init__()
        self._rec = rec
        self._channel = channel
        self._stream_kw = dict(channel=channel, sample_bits=sample_bits,
                               transport=transport, crop=crop, batch=batch)
        self._avg_only = avg_only

        self._lock = threading.Lock()
        self._latest = None                       # latest volts ndarray
        self._history: deque[np.ndarray] = deque(maxlen=avg_window)
        self._frames = 0                          # total frames received
        self._err = None
        self._stop = threading.Event()
        self._avg_window = avg_window
        self._start = time.monotonic()

        self.setWindowTitle(f"rigol-fastrec stream (CH{channel}) -- drag to select range")
        self.resize(1200, 700)
        self._build_ui()

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._redraw)
        self._timer.start(33)                     # ~30 Hz redraw

    # --------------------------------------------------------------- UI

    def _build_ui(self):
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        self.setCentralWidget(central)
        pg.setConfigOptions(antialias=False, useOpenGL=True)

        self.region_label = QtWidgets.QLabel("region: (drag in plot)")
        self.region_label.setStyleSheet("font-family: monospace; font-size: 11pt;")
        layout.addWidget(self.region_label)

        self.plot = pg.PlotWidget()
        self.plot.setBackground("#111")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setLabel("bottom", "sample")
        self.plot.setLabel("left", "volts")
        layout.addWidget(self.plot, stretch=1)

        self.cur_curve = (None if self._avg_only
                          else self.plot.plot(pen=pg.mkPen("#ffd740", width=1)))
        self.avg_curve = self.plot.plot(pen=pg.mkPen("#26c6da", width=2))

        self.region = pg.LinearRegionItem(
            values=[0, 100],
            brush=pg.mkBrush(64, 196, 255, 30),
            hoverBrush=pg.mkBrush(64, 196, 255, 60),
        )
        self.region.setZValue(-10)
        self.plot.addItem(self.region)
        self.region.sigRegionChanged.connect(self._on_region_changed)

        self.status = QtWidgets.QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("waiting for frames... (is the scope triggering?)")

    def _on_region_changed(self):
        lo, hi = sorted(int(v) for v in self.region.getRegion())
        self.region_label.setText(
            f"region: samples {lo}..{hi}  (width={hi - lo})   "
            f"-> --crop {max(0, lo)},{hi}"
        )

    # --------------------------------------------------------------- stream

    def _read_loop(self):
        """Background: pull frames off the stream, convert to volts, stash the
        latest + a rolling history. Runs until _stop or the socket closes."""
        try:
            for frame in self._rec.stream(**self._stream_kw):
                if self._stop.is_set():
                    break
                volts = self._rec.to_volts(frame, channel=self._channel)
                with self._lock:
                    self._latest = volts
                    self._history.append(volts)
                    self._frames += 1
        except Exception as e:           # noqa: BLE001 — surfaced in the status bar
            with self._lock:
                self._err = e

    def _redraw(self):
        with self._lock:
            v = self._latest
            frames = self._frames
            err = self._err
            hist = list(self._history) if len(self._history) >= 3 else None

        if err is not None:
            self.status.showMessage(f"stream error: {err}")
            return
        if v is None:
            return

        if self.cur_curve is not None:
            self.cur_curve.setData(v)
        if hist is not None:
            self.avg_curve.setData(np.mean(np.asarray(hist), axis=0))

        if frames == 1:
            self.region.setRegion([0, len(v)])
            self.plot.enableAutoRange()

        elapsed = max(1e-3, time.monotonic() - self._start)
        rate = frames / elapsed
        peak = float(np.abs(v).max())
        self.status.showMessage(
            f"frames: {frames}   rate: {rate:6.1f}/s   peak: {peak:.4f} V   "
            f"avg window: {min(frames, self._avg_window)}/{self._avg_window}"
        )

    # --------------------------------------------------------------- shutdown

    def closeEvent(self, event):
        self._stop.set()
        try:
            self._timer.stop()
        except Exception:
            pass
        # The scope teardown (which restores the live display) happens in main's
        # `with WaveRecorder` __exit__; that closes the data socket, unblocking
        # the daemon reader's recv. Nothing else to do here.
        event.accept()


def _install_sigint_handler(app):
    """Qt eats SIGINT; wire Ctrl-C to a clean quit (-> closeEvent -> teardown)."""
    def handler(signum, frame):
        print("\nCtrl-C, shutting down...", file=sys.stderr)
        app.quit()
    signal.signal(signal.SIGINT, handler)
    keep = QtCore.QTimer()
    keep.start(200)
    keep.timeout.connect(lambda: None)
    app._sigint_keepalive = keep


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", required=True, help="scope IP / hostname")
    p.add_argument("--channel", type=int, default=1, help="channel to view (default 1)")
    p.add_argument("--samples", type=int, default=1000, help="samples/frame (MDEP)")
    p.add_argument("--sample-rate", type=float, default=1e9, help="Sa/s (default 1e9)")
    p.add_argument("--range", type=float, default=1.0,
                   help="vertical full-scale volts (default 1.0)")
    p.add_argument("--trigger-source", default=None,
                   help="trigger channel (default: the viewed channel, CHANn)")
    p.add_argument("--trigger-level", type=float, default=0.0, help="trigger level V")
    p.add_argument("--trigger-slope", default="POS", choices=["POS", "NEG"])
    p.add_argument("--auto", action="store_true", default=True,
                   help="self-trigger (AUTO sweep) -- free-runs with no DUT [default]")
    p.add_argument("--no-auto", dest="auto", action="store_false",
                   help="capture only on real triggers (NORM sweep)")
    p.add_argument("--crop", default=None, metavar="LO,HI",
                   help="sample window lo,hi (agent-side crop)")
    p.add_argument("--sample-bits", type=int, default=16, choices=[16, 8])
    p.add_argument("--transport", default="raw", choices=["raw", "packed"])
    p.add_argument("--batch", type=int, default=0,
                   help="frames per FPGA capture (0 = hardware max)")
    p.add_argument("--avg-window", type=int, default=50)
    p.add_argument("--avg-only", action="store_true")
    args = p.parse_args()

    crop = None
    if args.crop:
        lo, hi = (int(x) for x in args.crop.split(","))
        crop = (lo, hi)
    trig_src = args.trigger_source or f"CHAN{args.channel}"

    app = QtWidgets.QApplication(sys.argv)
    _install_sigint_handler(app)

    with WaveRecorder(host=args.host) as rec:
        rec.configure(
            samples=args.samples, sample_rate=args.sample_rate,
            trigger=Trigger(source=trig_src, level=args.trigger_level,
                            slope=args.trigger_slope),
            channels={args.channel: Channel(range=args.range)},
        )
        # AUTO sweep self-triggers so frames flow with no DUT; NORM waits for
        # real triggers. The agent's capture loop paces off whichever this sets.
        rec.scpi.write(":TRIGger:SWEep " + ("AUTO" if args.auto else "NORMal"))
        viewer = StreamViewer(
            rec, channel=args.channel, sample_bits=args.sample_bits,
            transport=args.transport, crop=crop, batch=args.batch,
            avg_window=args.avg_window, avg_only=args.avg_only)
        viewer.show()
        app.aboutToQuit.connect(viewer.close)
        app.exec()


if __name__ == "__main__":
    main()
