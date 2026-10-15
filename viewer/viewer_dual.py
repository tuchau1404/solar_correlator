#!/usr/bin/env python3
"""Dual-SDR SOL2 spectrum & waterfall diagnostic monitor (S11 & S22)."""
import argparse
from datetime import datetime, timezone, timedelta
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

# Múi giờ Việt Nam: UTC+7
TZ_VN = timezone(timedelta(hours=7))


class ChannelPanel:
    """Quản lý đồ thị phổ 1D, thác nước 2D, ColorBar và bộ đệm vòng của 1 kênh SDR."""

    def __init__(self, graphics, col_plot, col_bar, label_tag, power_symbol, pen_color, freq):
        self.label_tag = label_tag
        self.power_symbol = power_symbol
        self.freq = freq

        self.spectrum = graphics.addPlot(row=0, col=col_plot)
        self.waterfall = graphics.addPlot(row=1, col=col_plot)

        for plot in (self.spectrum, self.waterfall):
            plot.setLabel('bottom', 'Frequency', units='MHz')
            plot.getAxis('bottom').enableAutoSIPrefix(False)
            plot.getAxis('left').setWidth(90)
            plot.setXRange(START - STEP / 2, STOP - STEP / 2, padding=0)

        self.spectrum.setTitle(f'{self.label_tag} ({self.power_symbol}) — Waiting for data...')
        self.spectrum.setLabel('left', self.power_symbol, units='dB re 1 ADC-count²/bin')
        self.spectrum.getAxis('left').enableAutoSIPrefix(False)
        self.spectrum.setYRange(10, 95)
        self.spectrum.showGrid(x=True, y=True, alpha=0.2)

        self.waterfall.setLabel('left', 'Frame slots (newest at 0)')
        self.waterfall.setYRange(-HISTORY, 0, padding=0)
        self.waterfall.setXLink(self.spectrum)

        self.curve = self.spectrum.plot(pen=pg.mkPen(pen_color, width=1))

        self.ring = np.full((HISTORY, BINS), np.nan, dtype=np.float32)
        self.display = self.ring.copy()

        self.image = pg.ImageItem(self.display, axisOrder='row-major', levels=(20, 85))
        self.waterfall.addItem(self.image)
        # Căn tâm pixel thác nước trùng khít với tọa độ bin FFT 1D
        self.image.setRect(QtCore.QRectF(START - STEP / 2, -HISTORY, STOP - START, HISTORY))

        self.colorbar = pg.ColorBarItem(
            values=(20, 85),
            colorMap=pg.colormap.get('inferno'),
            label='dB re 1 ADC-count²/bin',
            interactive=True,
        )
        self.colorbar.setImageItem(self.image)
        graphics.addItem(self.colorbar, row=1, col=col_bar)

        self.write = 0
        self.last_session = None
        self.last_id = None
        self.latest = None
        self.arrived = None
        self.peak_db = None
        self.peak_freq = None

    def append(self, values):
        self.ring[self.write] = values
        self.write = (self.write + 1) % HISTORY

    def process_receiver(self, receiver):
        frames, stats, skips, error = receiver.take()
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
            self.peak_db = float(values[peak])
            self.peak_freq = float(self.freq[peak])

            dt_vn = datetime.fromtimestamp(stamp / 1e6, tz=timezone.utc).astimezone(TZ_VN)
            time_str = dt_vn.strftime('%H:%M:%S.%f')[:-3]

            self.spectrum.setTitle(
                f'{self.label_tag} | Frame {frame} (M={count}) | '
                f'Peak {self.peak_db:.2f} dB @ {self.peak_freq:.6f} MHz | Giờ VN: {time_str}'
            )

        return stats, skips, error


class DualViewer(QtWidgets.QMainWindow):
    def __init__(self, receiver1, receiver2, port1, port2):
        super().__init__()
        self.receiver1 = receiver1
        self.receiver2 = receiver2
        self.port1 = port1
        self.port2 = port2

        self.setWindowTitle(
            f'Solar Correlator — Dual SDR Live Waterfall Diagnostic (UDP {port1} & {port2})'
        )
        self.resize(1820, 880)

        graphics = pg.GraphicsLayoutWidget()
        self.setCentralWidget(graphics)

        self.freq = START + np.arange(BINS) * STEP

        # Cột Trái (col=0 & col=1): SDR 1 (Channel 1 - S11)
        self.ch1 = ChannelPanel(
            graphics=graphics,
            col_plot=0,
            col_bar=1,
            label_tag=f'SDR 1 [:{port1}]',
            power_symbol='|S11|',
            pen_color='#ffdc32',
            freq=self.freq,
        )

        # Cột Phải (col=2 & col=3): SDR 2 (Channel 2 - S22)
        self.ch2 = ChannelPanel(
            graphics=graphics,
            col_plot=2,
            col_bar=3,
            label_tag=f'SDR 2 [:{port2}]',
            power_symbol='|S22|',
            pen_color='#38bdf8',
            freq=self.freq,
        )

        # Khóa trục tần số X giữa cả 4 đồ thị (ch1.waterfall và ch2.waterfall đã link với spectrum của chúng)
        self.ch2.spectrum.setXLink(self.ch1.spectrum)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_data)
        self.timer.start(25)

    def update_data(self):
        stats1, skips1, err1 = self.ch1.process_receiver(self.receiver1)
        stats2, skips2, err2 = self.ch2.process_receiver(self.receiver2)

        age1 = 'wait' if self.ch1.arrived is None else f'{time.monotonic() - self.ch1.arrived:.1f}s'
        age2 = 'wait' if self.ch2.arrived is None else f'{time.monotonic() - self.ch2.arrived:.1f}s'

        if self.ch1.peak_db is not None and self.ch2.peak_db is not None:
            delta_peak = self.ch1.peak_db - self.ch2.peak_db
            delta_str = f'ΔPeak(Ch1-Ch2) = {delta_peak:+.2f} dB'
        else:
            delta_str = 'ΔPeak = N/A'

        msg1 = (
            f'Ch1[:{self.port1}] ({age1}): complete={stats1.get("complete", 0)} '
            f'gaps={stats1.get("frame_gaps", 0)} incomplete={stats1.get("incomplete", 0)} '
            f'late={stats1.get("late", 0)} invalid={stats1.get("invalid", 0)} '
            f'skips={skips1} RCVBUF={self.receiver1.actual_rcvbuf}'
        )
        msg2 = (
            f'Ch2[:{self.port2}] ({age2}): complete={stats2.get("complete", 0)} '
            f'gaps={stats2.get("frame_gaps", 0)} incomplete={stats2.get("incomplete", 0)} '
            f'late={stats2.get("late", 0)} invalid={stats2.get("invalid", 0)} '
            f'skips={skips2} RCVBUF={self.receiver2.actual_rcvbuf}'
        )

        errors = []
        if err1:
            errors.append(f'Ch1 ERR: {err1}')
        if err2:
            errors.append(f'Ch2 ERR: {err2}')
        err_suffix = (' | ' + ' ; '.join(errors)) if errors else ''

        self.statusBar().showMessage(f'{msg1}  ||  {msg2}  ||  {delta_str}{err_suffix}')

    def closeEvent(self, event):
        self.timer.stop()
        self.receiver1.close()
        self.receiver2.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser(
        description='Dual SDR Live Waterfall Spectrogram Viewer (SOL2 v2)'
    )
    parser.add_argument('--bind', default='0.0.0.0', help='Local bind IPv4 address')
    parser.add_argument('--port1', type=int, default=9998, help='UDP port for SDR 1 (S11)')
    parser.add_argument('--port2', type=int, default=9999, help='UDP port for SDR 2 (S22)')
    parser.add_argument('--source-ip', help='Accept datagrams only from this sender IPv4')
    args = parser.parse_args()

    if args.port1 == args.port2:
        parser.error('--port1 and --port2 must be distinct UDP ports.')

    app = QtWidgets.QApplication(sys.argv[:1])
    pg.setConfigOptions(antialias=False, imageAxisOrder='row-major')

    receiver1 = Receiver(args.bind, args.port1, args.source_ip)
    receiver2 = Receiver(args.bind, args.port2, args.source_ip)
    try:
        window = DualViewer(receiver1, receiver2, args.port1, args.port2)
        receiver1.start()
        receiver2.start()
        signal.signal(signal.SIGINT, lambda *_: window.close())
        signal.signal(signal.SIGTERM, lambda *_: window.close())
        window.show()
        return app.exec_()
    finally:
        receiver1.close()
        receiver2.close()


if __name__ == '__main__':
    sys.exit(main())