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

        self.waterfall.setLabel('left', 'Frame slots (newest row: -1 to 0)')
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

    def reset(self):
        self.ring.fill(np.nan)
        self.write = 0
        self.latest = None
        self.arrived = None
        self.peak_db = self.peak_freq = None
        self.curve.setData([], [])
        self.render()

    def render(self):
        n = HISTORY - self.write
        self.display[:n] = self.ring[self.write:]
        self.display[n:] = self.ring[:self.write]
        self.image.setImage(self.display, autoLevels=False)
        if self.latest is None:
            self.spectrum.setTitle(f'{self.label_tag} | Waiting / missing paired frame')
            self.curve.setData([], [])
            return
        frame, stamp, count, values = self.latest
        self.curve.setData(self.freq, values)
        peak = int(np.argmax(values))
        self.peak_db = float(values[peak])
        self.peak_freq = float(self.freq[peak])
        try:
            clock = datetime.fromtimestamp(stamp / 1e6, tz=TZ_VN).strftime('%H:%M:%S.%f')[:-3]
        except (OverflowError, OSError, ValueError):
            clock = 'invalid timestamp'
        self.spectrum.setTitle(
            f'{self.label_tag} | Frame {frame} (M={count}) | '
            f'Peak {self.peak_db:.2f} dB @ {self.peak_freq:.6f} MHz | VN: {clock}'
        )


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

        self.pending = [{}, {}]
        self.sessions = [None, None]
        self.last_received = [None, None]
        self.last_arrival = [None, None]
        self.last_committed = None
        self.delta = None
        self.pair_wait = 0.35
        self.local_late = self.unpaired = self.pair_drops = self.metadata_mismatch = 0
        self.last_status = 0.0
        self.snapshots = [({}, 0, None), ({}, 0, None)]
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_data)
        self.timer.start(25)

    def reset_pairing(self):
        self.pending = [{}, {}]
        self.last_committed = None
        self.delta = None
        for panel in (self.ch1, self.ch2):
            panel.reset()

    def ingest(self, channel, frames, now):
        for session, frame, stamp, count, values in frames:
            if session != self.sessions[channel]:
                if self.sessions[channel] is not None:
                    self.reset_pairing()
                self.sessions[channel] = session
                self.last_received[channel] = None
            previous = self.last_received[channel]
            if previous is not None and frame <= previous:
                self.local_late += 1
                continue
            self.last_received[channel] = frame
            if self.last_committed is not None and frame <= self.last_committed:
                self.local_late += 1
                continue
            # Receiver has already checked shape/finiteness and integration count.
            self.pending[channel][frame] = (stamp, count, values, now)
            self.last_arrival[channel] = now
            # Receiver.take() returns at most 300 items. Keep only a small tail;
            # discarded slots appear as NaN when the next frame is committed.
            while len(self.pending[channel]) > 32:
                del self.pending[channel][min(self.pending[channel])]
                self.pair_drops += 1

    def commit_ready(self, now):
        dirty = False
        while self.pending[0] or self.pending[1]:
            frame = min(set(self.pending[0]) | set(self.pending[1]))
            one = self.pending[0].get(frame)
            two = self.pending[1].get(frame)
            matched = one is not None and two is not None and one[:2] == two[:2]
            arrival = min(item[3] for item in (one, two) if item is not None)
            if not matched and now - arrival < self.pair_wait:
                break
            self.pending[0].pop(frame, None)
            self.pending[1].pop(frame, None)
            if self.last_committed is not None and frame <= self.last_committed:
                self.local_late += 1
                continue
            gap = 0 if self.last_committed is None else frame - self.last_committed - 1
            for panel in (self.ch1, self.ch2):
                if gap >= HISTORY:
                    panel.ring.fill(np.nan)
                    panel.write = 0
                else:
                    for _ in range(gap):
                        panel.append(np.nan)
            if one is not None and two is not None and not matched:
                # Equal id but unequal timestamp/count is not the same cycle.
                self.metadata_mismatch += 1
                one = two = None
            for panel, item in zip((self.ch1, self.ch2), (one, two)):
                if item is None:
                    panel.append(np.nan)
                    panel.latest = None
                    panel.peak_db = panel.peak_freq = None
                else:
                    stamp, count, values, arrived = item
                    panel.append(values)
                    panel.latest = (frame, stamp, count, values)
                    panel.arrived = arrived
            if matched:
                self.delta = (frame, float(np.max(one[2]) - np.max(two[2])), now)
            else:
                self.delta = None
                self.unpaired += 1
            self.last_committed = frame
            dirty = True
        if dirty:
            self.ch1.render()
            self.ch2.render()
        return dirty

    def update_data(self):
        now = time.monotonic()
        packets = [self.receiver1.take(), self.receiver2.take()]
        for channel, (frames, stats, skips, error) in enumerate(packets):
            self.ingest(channel, frames, now)
            self.snapshots[channel] = (stats, skips, error)
        dirty = self.commit_ready(now)
        if not dirty and now - self.last_status < 0.2:
            return
        self.last_status = now
        pieces = []
        for idx, (receiver, port) in enumerate(((self.receiver1, self.port1),
                                              (self.receiver2, self.port2))):
            stats, skips, error = self.snapshots[idx]
            age = 'wait' if self.last_arrival[idx] is None else f'{now-self.last_arrival[idx]:.1f}s'
            pieces.append(
                f'Ch{idx+1}[:{port}] age={age} complete={stats.get("complete", 0)} '
                f'gaps={stats.get("frame_gaps", 0)} incomplete={stats.get("incomplete", 0)} '
                f'late={stats.get("late", 0)} invalid={stats.get("invalid", 0)} '
                f'skips={skips} RCVBUF={receiver.actual_rcvbuf}'
                + (f' ERROR: {error}' if error else '')
            )
        if self.delta is not None and now - self.delta[2] <= 1.0:
            frame, value, _ = self.delta
            delta_text = f'ΔPeak={value:+.2f} dB (paired frame {frame})'
        else:
            delta_text = 'ΔPeak=N/A (unpaired/stale)'
        pieces.append(f'{delta_text}; unpaired={self.unpaired} pair_drops={self.pair_drops} '
                      f'late_ui={self.local_late} metadata_mismatch={self.metadata_mismatch}')
        self.statusBar().showMessage(' | '.join(pieces))

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

    if not (1 <= args.port1 <= 65535 and 1 <= args.port2 <= 65535):
        parser.error('Ports must be in 1..65535')
    if args.port1 == args.port2:
        parser.error('--port1 and --port2 must be distinct UDP ports.')

    app = QtWidgets.QApplication(sys.argv[:1])
    pg.setConfigOptions(antialias=False, imageAxisOrder='row-major')

    receiver1 = receiver2 = None
    try:
        receiver1 = Receiver(args.bind, args.port1, args.source_ip)
        receiver2 = Receiver(args.bind, args.port2, args.source_ip)
        window = DualViewer(receiver1, receiver2, args.port1, args.port2)
        receiver1.start()
        receiver2.start()
        signal.signal(signal.SIGINT, lambda *_: window.close())
        signal.signal(signal.SIGTERM, lambda *_: window.close())
        window.show()
        return app.exec_()
    finally:
        if receiver1 is not None:
            receiver1.close()
        if receiver2 is not None:
            receiver2.close()


if __name__ == '__main__':
    sys.exit(main())
