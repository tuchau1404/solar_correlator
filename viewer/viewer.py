#!/usr/bin/env python3
"""SOL2 spectrum monitor. Display history is frame-indexed, not wall-clock age."""
import argparse
import signal
import sys
import time
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets
from telemetry_protocol import Receiver, BINS

HISTORY = 300
START, STOP = 30.0, 40.0
STEP = (STOP - START) / BINS


class Viewer(QtWidgets.QMainWindow):
    def __init__(self, receiver):
        super().__init__()
        self.receiver = receiver
        self.setWindowTitle('Solar Correlator — SOL2')
        self.resize(1200, 820)
        graphics = pg.GraphicsLayoutWidget()
        self.setCentralWidget(graphics)
        self.spectrum = graphics.addPlot(row=0, col=0)
        self.waterfall = graphics.addPlot(row=1, col=0)
        for plot in (self.spectrum, self.waterfall):
            plot.setLabel('bottom', 'Frequency', units='MHz')
            plot.getAxis('bottom').enableAutoSIPrefix(False)
            plot.getAxis('left').setWidth(90)
            plot.setXRange(START - STEP / 2, STOP - STEP / 2, padding=0)
        self.spectrum.setLabel('left', '|S12|', units='dB re 1 ADC-count²/bin')
        self.spectrum.getAxis('left').enableAutoSIPrefix(False)
        self.spectrum.setYRange(10, 95)
        self.spectrum.showGrid(x=True, y=True, alpha=0.2)
        self.waterfall.setLabel('left', 'Frame slots (newest at 0)')
        self.waterfall.setYRange(-HISTORY, 0, padding=0)
        self.waterfall.setXLink(self.spectrum)
        self.curve = self.spectrum.plot(pen=pg.mkPen('#ffdc32', width=1))
        self.freq = START + np.arange(BINS) * STEP
        self.ring = np.full((HISTORY, BINS), np.nan, dtype=np.float32)
        self.display = self.ring.copy()
        self.image = pg.ImageItem(self.display, axisOrder='row-major', levels=(20, 85))
        self.waterfall.addItem(self.image)
        # Pixel CENTERS coincide with the 1D FFT-bin coordinates.
        self.image.setRect(QtCore.QRectF(START - STEP / 2, -HISTORY, STOP - START, HISTORY))
        self.colorbar = pg.ColorBarItem(values=(20, 85), colorMap=pg.colormap.get('inferno'),
                                       label='dB re 1 ADC-count²/bin', interactive=True)
        self.colorbar.setImageItem(self.image)
        graphics.addItem(self.colorbar, row=1, col=1)
        self.write = 0
        self.last_session = self.last_id = None
        self.latest = None
        self.arrived = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_data)
        self.timer.start(25)

    def append(self, values):
        self.ring[self.write] = values
        self.write = (self.write + 1) % HISTORY

    def update_data(self):
        frames, stats, skips, error = self.receiver.take()
        for session, frame, stamp, count, values in frames:
            if session != self.last_session:
                self.ring.fill(np.nan)
                self.write = 0
                self.last_id = None
                self.last_session = session
            if self.last_id is not None:
                gap = min(HISTORY, max(0, frame - self.last_id - 1))
                if gap == HISTORY:
                    self.ring.fill(np.nan)
                else:
                    for _ in range(gap):
                        self.append(np.nan)
            self.append(values)
            self.last_id = frame
            self.latest = (frame, stamp, count, values)
            self.arrived = time.monotonic()
        if frames:
            n = HISTORY - self.write
            self.display[:n] = self.ring[self.write:]
            self.display[n:] = self.ring[:self.write]
            self.image.setImage(self.display, autoLevels=False)
            frame, stamp, count, values = self.latest
            self.curve.setData(self.freq, values)
            peak = int(np.argmax(values))
            self.spectrum.setTitle(f'Frame {frame} | M={count} | peak {values[peak]:.2f} dB '
                                   f'@ {self.freq[peak]:.6f} MHz | UTC µs={stamp}')
        age = 'waiting' if self.arrived is None else f'{time.monotonic() - self.arrived:.1f}s since GUI update'
        self.statusBar().showMessage(
            f'{age} | complete={stats.get("complete", 0)} gaps={stats.get("frame_gaps", 0)} '
            f'incomplete={stats.get("incomplete", 0)} late={stats.get("late", 0)} '
            f'invalid={stats.get("invalid", 0)} display_skips={skips} '
            f'SO_RCVBUF={self.receiver.actual_rcvbuf}' + (f' | ERROR: {error}' if error else ''))

    def closeEvent(self, event):
        self.timer.stop()
        self.receiver.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9999)
    parser.add_argument('--source-ip', help='Accept datagrams only from this sender IPv4')
    args = parser.parse_args()
    app = QtWidgets.QApplication(sys.argv[:1])
    pg.setConfigOptions(antialias=False, imageAxisOrder='row-major')
    receiver = Receiver(args.bind, args.port, args.source_ip)
    try:
        window = Viewer(receiver)
        receiver.start()
        signal.signal(signal.SIGINT, lambda *_: window.close())
        signal.signal(signal.SIGTERM, lambda *_: window.close())
        window.show()
        return app.exec_()
    finally:
        receiver.close()


if __name__ == '__main__':
    sys.exit(main())
