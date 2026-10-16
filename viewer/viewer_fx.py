#!/usr/bin/env python3
"""SOL2 v3 CW / solar display, bounded frame matching and phase quality masking.

Legacy v2 remains in viewer.py / viewer_dual.py and is rejected here.
"""
import argparse
from datetime import datetime, timezone, timedelta
import signal
import sys
import time
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtWidgets
from telemetry_protocol import Receiver, BINS, FrameJoiner, S12, S11, S22, PHASE, COHERENCE
from science_model import ObservationHistory

HISTORY = 300
START, STOP = 30.0, 40.0
STEP = (STOP - START) / BINS

# Múi giờ Việt Nam: UTC+7
TZ_VN = timezone(timedelta(hours=7))


class WaterfallBuffer:
    """Bộ đệm vòng 2D không dùng np.roll, hỗ trợ điền NaN khi mất gói hoặc đổi phiên."""

    def __init__(self, history, bins):
        self.history = history
        self.bins = bins
        self.ring = np.full((history, bins), np.nan, dtype=np.float32)
        self.display = self.ring.copy()
        self.write = 0
        self.last_session = None
        self.last_id = None

    def reset(self):
        self.ring.fill(np.nan)
        self.write = 0
        self.last_id = None

    def append(self, values):
        self.ring[self.write] = values
        self.write = (self.write + 1) % self.history

    def push_frame(self, session, frame_id, values):
        if session != self.last_session:
            self.reset()
            self.last_session = session
        if self.last_id is not None:
            gap = min(self.history, max(0, frame_id - self.last_id - 1))
            if gap == self.history:
                self.ring.fill(np.nan)
            else:
                for _ in range(gap):
                    self.append(np.nan)
        self.append(values)
        self.last_id = frame_id

    def render_view(self):
        n = self.history - self.write
        self.display[:n] = self.ring[self.write:]
        self.display[n:] = self.ring[:self.write]
        return self.display


class DeepIntegrator:
    """Bộ tích phân sâu miền công suất tuyến tính từ các khung phổ dB liên tiếp."""

    def __init__(self, group_size, bins):
        self.group_size = max(1, int(group_size))
        self.bins = bins
        self.accum_linear = np.zeros(bins, dtype=np.float64)
        self.count = 0
        self.deep_frame_id = 0
        self.last_session = None
        self.last_frame_id = None

    def reset(self):
        self.accum_linear.fill(0.0)
        self.count = 0
        self.last_frame_id = None

    def push(self, session, frame_id, stamp, m_frames, db_values):
        completed = []
        if session != self.last_session:
            self.reset()
            self.deep_frame_id = 0
            self.last_session = session

        if self.last_frame_id is not None and frame_id > self.last_frame_id + 1:
            gap_frames = frame_id - self.last_frame_id - 1
            self.accum_linear.fill(0.0)
            self.count = 0
            missed_groups = min(HISTORY, gap_frames // self.group_size)
            for _ in range(missed_groups):
                self.deep_frame_id += 1
                completed.append(
                    (
                        session,
                        self.deep_frame_id,
                        stamp,
                        m_frames * self.group_size,
                        np.full(self.bins, np.nan, dtype=np.float32),
                    )
                )

        self.last_frame_id = frame_id
        linear_power = np.power(10.0, np.asarray(db_values, dtype=np.float64) * 0.1)
        self.accum_linear += linear_power
        self.count += 1

        if self.count >= self.group_size:
            mean_linear = np.maximum(self.accum_linear / float(self.count), 1e-12)
            deep_db = (10.0 * np.log10(mean_linear)).astype(np.float32)
            self.deep_frame_id += 1
            completed.append(
                (
                    session,
                    self.deep_frame_id,
                    stamp,
                    m_frames * self.count,
                    deep_db,
                )
            )
            self.accum_linear.fill(0.0)
            self.count = 0

        return completed


class VietnamTimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [datetime.fromtimestamp(v, TZ_VN).strftime('%H:%M:%S')
                if np.isfinite(v) and v > 0 else '' for v in values]


class SlotTimeAxis(pg.AxisItem):
    """Frame slots can be irregular in UTC; label actual received timestamps."""
    def __init__(self, model, **kwargs):
        super().__init__(**kwargs)
        self.model = model

    def tickStrings(self, values, scale, spacing):
        rows = list(self.model.rows)
        labels = []
        for v in values:
            i = int(np.floor(v)) - (self.model.capacity - len(rows))
            row = rows[i] if 0 <= i < len(rows) else None
            labels.append(datetime.fromtimestamp(row[2]/1e6, TZ_VN).strftime('%H:%M:%S') if row else '')
        return labels


class FXViewer(QtWidgets.QMainWindow):
    def __init__(self, receivers, mode='cw', deep_frames=5, frequency=35.5, threshold=.5):
        super().__init__()
        self.receivers = receivers
        self.model = ObservationHistory(HISTORY, frequency)
        self.joiner = FrameJoiner(enabled=receivers)
        self.integrator = DeepIntegrator(deep_frames, BINS)
        self.buf_deep = WaterfallBuffer(HISTORY, BINS)
        self.setWindowTitle('Solar Correlator — SOL2 v3 | CW / Solar')
        self.resize(1480, 1000)
        root = QtWidgets.QWidget(); layout = QtWidgets.QVBoxLayout(root)
        controls = QtWidgets.QHBoxLayout(); layout.addLayout(controls)
        self.mode = QtWidgets.QComboBox(); self.mode.addItems(['CW', 'Solar'])
        self.mode.setCurrentText(mode.capitalize() if mode == 'solar' else 'CW')
        controls.addWidget(self.mode)
        controls.addWidget(QtWidgets.QLabel('Requested MHz:'))
        self.frequency = QtWidgets.QDoubleSpinBox(); self.frequency.setDecimals(6)
        self.frequency.setRange(START, STOP-STEP); self.frequency.setSingleStep(STEP)
        self.frequency.setValue(frequency); controls.addWidget(self.frequency)
        self.bin_label = QtWidgets.QLabel(); controls.addWidget(self.bin_label)
        controls.addWidget(QtWidgets.QLabel('Display mask γ ≥'))
        self.threshold = QtWidgets.QDoubleSpinBox(); self.threshold.setRange(0, 1)
        self.threshold.setSingleStep(.05); self.threshold.setValue(threshold)
        controls.addWidget(self.threshold)
        controls.addWidget(QtWidgets.QLabel('(display threshold; γ=0 always invalid)'))
        self.graphics = g = pg.GraphicsLayoutWidget(); layout.addWidget(g)
        self.setCentralWidget(root)
        self.freq = START + np.arange(BINS) * STEP
        self.spectrum_plot = g.addPlot(row=0, col=0, colspan=2, title='Matched S11 / S22 / |S12| spectrum')
        self.spectrum_plot.setLabel('bottom', 'Frequency', units='MHz')
        self.spectrum_plot.setLabel('left', 'Power', units='dB')
        self.spectrum_plot.setTitle('Matched S11 / S22 / |S12| — dB re 1 ADC-count²/bin')
        self.spectrum_plot.setXRange(START-STEP/2, STOP-STEP/2, padding=0)
        self.spectrum_plot.addLegend()
        self.curves = {kind: self.spectrum_plot.plot(pen=pg.mkPen(color, width=1), name=name)
                       for kind, color, name in [(S12, '#ffdc32', '|S12|'), (S11, '#38bdf8', 'S11'), (S22, '#f87171', 'S22')]}
        self.deep_curve = self.spectrum_plot.plot(pen='#f97316', name='Mean |S12| (linear power average)')
        self.marker = pg.InfiniteLine(pos=self.model.actual, angle=90, pen='#a78bfa')
        self.spectrum_plot.addItem(self.marker)
        self.spectrum_plot.scene().sigMouseClicked.connect(self.spectrum_clicked)
        self.phase_plot = g.addPlot(row=1, col=0, colspan=2, axisItems={'bottom': VietnamTimeAxis(orientation='bottom')})
        self.phase_plot.setLabel('bottom', 'Received UTC timestamp → Vietnam UTC+07:00')
        self.phase_plot.setLabel('left', 'Phase', units='°')
        self.phase_plot.setYRange(-180, 180, padding=0)
        self.phase_curve = self.phase_plot.plot(pen='#a78bfa', connect='finite')
        self.phase_points = pg.ScatterPlotItem(size=3, pen=None, brush='#a78bfa')
        self.phase_plot.addItem(self.phase_points)
        self.gamma_plot = g.addPlot(row=2, col=0, colspan=2, axisItems={'bottom': VietnamTimeAxis(orientation='bottom')})
        self.gamma_plot.setLabel('left', 'γ amplitude')
        self.gamma_plot.setLabel('bottom', 'Vietnam UTC+07:00')
        self.gamma_plot.setYRange(0, 1.05); self.gamma_plot.setXLink(self.phase_plot)
        self.gamma_curve = self.gamma_plot.plot(pen='#34d399', connect='finite')
        self.mask_line = pg.InfiniteLine(pos=threshold, angle=0, pen='#ef4444'); self.gamma_plot.addItem(self.mask_line)
        self.wf_fast_plot, self.img_fast, self.cbar_fast = self.make_waterfall(3, '|S12| dB', False)
        self.wf_phase_plot, self.img_phase, self.cbar_phase = self.make_waterfall(4, 'Phase ° — masked pixels transparent', True)
        self.wf_deep_plot = g.addPlot(row=5, col=0, title=f'Mean |S12|, {deep_frames} frames/slot (not coherent integration)')
        self.wf_deep_plot.setLabel('left', 'Deep slots')
        self.wf_deep_plot.setLabel('bottom', 'Frequency', units='MHz')
        self.wf_deep_plot.setXLink(self.spectrum_plot)
        self.wf_deep_plot.enableAutoRange(x=False, y=False)
        self.wf_deep_plot.setYRange(-HISTORY, 0, padding=0)
        self.img_deep = pg.ImageItem(self.buf_deep.display, axisOrder='row-major', levels=(0, 1))
        self.wf_deep_plot.addItem(self.img_deep)
        self.img_deep.setRect(QtCore.QRectF(START-STEP/2, -HISTORY, STOP-START, HISTORY))
        self.cbar_deep = pg.ColorBarItem(values=(20, 85), colorMap=pg.colormap.get('inferno'), label='Mean |S12| dB', interactive=True)
        self.cbar_deep.setImageItem(self.img_deep); g.addItem(self.cbar_deep, row=5, col=1)
        for plot in (self.spectrum_plot, self.phase_plot, self.gamma_plot, self.wf_deep_plot):
            plot.getAxis('left').setWidth(110)
            plot.getAxis('bottom').enableAutoSIPrefix(False)
        self.spectrum_plot.setXRange(START-STEP/2, STOP-STEP/2, padding=0)
        self.frequency.valueChanged.connect(self.select_frequency)
        self.threshold.valueChanged.connect(self.render)
        self.mode.currentTextChanged.connect(self.apply_mode)
        self.select_frequency(self.frequency.value()); self.apply_mode()
        self.timer = QtCore.QTimer(self); self.timer.timeout.connect(self.update_data); self.timer.start(25)

    def make_waterfall(self, row, title, phase):
        plot = self.graphics.addPlot(row=row, col=0, title=title,
                                    axisItems={'left': SlotTimeAxis(self.model, orientation='left')})
        plot.setLabel('bottom', 'Frequency', units='MHz')
        plot.getAxis('bottom').enableAutoSIPrefix(False)
        plot.setLabel('left', 'VN time')
        plot.getAxis('left').setWidth(110)
        plot.setXLink(self.spectrum_plot); plot.setYRange(0, HISTORY, padding=0)
        plot.setMouseEnabled(x=True, y=False)
        plot.enableAutoRange(x=False, y=False)
        image = pg.ImageItem(np.full((HISTORY, BINS), np.nan, dtype=np.float32), axisOrder='row-major', levels=(0, 1)); plot.addItem(image)
        image.setRect(QtCore.QRectF(START-STEP/2, 0, STOP-START, HISTORY))
        if phase:
            import colorsys
            colors = [tuple(round(c*255) for c in colorsys.hsv_to_rgb(i/256, 1, 1)) for i in range(257)]
            colors[-1] = colors[0]
            cmap = pg.ColorMap(np.linspace(0, 1, 257), colors)
        else:
            cmap = pg.colormap.get('inferno')
        bar = pg.ColorBarItem(values=(-180, 180) if phase else (20, 85), colorMap=cmap,
                             label='Phase °' if phase else '|S12| dB', interactive=not phase)
        bar.setImageItem(image); self.graphics.addItem(bar, row=row, col=1)
        return plot, image, bar

    def select_frequency(self, frequency):
        self.model.select(frequency)
        self.marker.setValue(self.model.actual)
        self.bin_label.setText(f'Bin {self.model.bin} → {self.model.actual:.6f} MHz')
        self.render()

    def spectrum_clicked(self, event):
        if event.button() == QtCore.Qt.LeftButton and self.spectrum_plot.sceneBoundingRect().contains(event.scenePos()):
            frequency = self.spectrum_plot.vb.mapSceneToView(event.scenePos()).x()
            self.frequency.setValue(float(np.clip(frequency, START, STOP-STEP)))

    def apply_mode(self, *_):
        solar = self.mode.currentText() == 'Solar'
        for item in (self.wf_phase_plot, self.cbar_phase, self.wf_deep_plot, self.cbar_deep):
            item.setVisible(solar)
        self.graphics.ci.layout.setRowStretchFactor(1, 4 if not solar else 2)

    def update_data(self):
        reports = []
        incoming, complete = [], []
        for kind, receiver in self.receivers.items():
            frames, stats, skips, error = receiver.take()
            reports.append(f'{kind}:complete={stats.get("complete",0)} invalid={stats.get("invalid",0)} skips={skips}' + (f' ERROR {error}' if error else ''))
            incoming.extend((kind, frame) for frame in frames)
        # Interleave bounded receiver batches; draining one entire port first can
        # otherwise evict all matching S12 frames before AUX is visited.
        for kind, frame in sorted(incoming, key=lambda item: (item[1][2], item[1][0], item[1][1], item[0])):
            self.joiner.add(kind, frame)
            complete.extend(self.joiner.poll())
        complete.extend(self.joiner.poll())
        for observation in complete:
            self.model.push(observation)
            session, fid, stamp, count, streams = observation
            for deep in self.integrator.push(session, fid, stamp, count, streams[S12]):
                self.buf_deep.push_frame(deep[0], deep[1], deep[4])
                self.deep_curve.setData(self.freq, deep[4])
            for kind, curve in self.curves.items():
                curve.setData(self.freq, streams.get(kind, np.full(BINS, np.nan)))
        if complete:
            self.render()
        self.statusBar().showMessage(' | '.join(reports) + f' | join cache={len(self.joiner.pending)}/{self.joiner.capacity}')

    def render(self, *_):
        threshold = self.threshold.value()
        self.mask_line.setValue(threshold)
        x, phase, gamma = self.model.time_series(threshold)
        self.phase_curve.setData(x, phase, connect='finite')
        good = np.isfinite(x) & np.isfinite(phase)
        self.phase_points.setData(x[good], phase[good])
        self.gamma_curve.setData(x, gamma, connect='finite')
        self.img_fast.setImage(self.model.image(S12), autoLevels=False)
        self.img_phase.setImage(self.model.image(PHASE, threshold), autoLevels=False)
        self.img_deep.setImage(self.buf_deep.render_view(), autoLevels=False)
        self.wf_phase_plot.setTitle(f'Phase ° — display mask γ ≥ {threshold:.2f}; missing/invalid = transparent')
        latest = next((row for row in reversed(self.model.rows) if row is not None), None)
        stamp = datetime.fromtimestamp(latest[2]/1e6, TZ_VN).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] if latest else 'waiting'
        self.phase_plot.setTitle(f'Bin {self.model.bin}, {self.model.actual:.6f} MHz — wrapped phase | VN {stamp}')
        # Timestamp labels refer to the current ring contents, not cached ticks.
        for plot in (self.wf_fast_plot, self.wf_phase_plot):
            plot.getAxis('left').picture = None; plot.getAxis('left').update()

    def closeEvent(self, event):
        self.timer.stop()
        for receiver in self.receivers.values():
            receiver.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description='Typed SOL2 v3 CW / Solar viewer; legacy v2 is rejected')
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', '--port-s12', dest='port_s12', type=int, default=9999)
    parser.add_argument('--port-s11', type=int, default=9998)
    parser.add_argument('--port-s22', type=int, default=9997)
    parser.add_argument('--port-phase', type=int, default=9996)
    parser.add_argument('--port-coherence', type=int, default=9995)
    parser.add_argument('--port-aux', help=argparse.SUPPRESS)
    parser.add_argument('--aux-mode', help=argparse.SUPPRESS)
    parser.add_argument('--mode', choices=('cw', 'solar'), default='cw')
    parser.add_argument('--frequency-mhz', type=float, default=35.5)
    parser.add_argument('--coherence-threshold', type=float, default=.5, help='Adjustable display mask, not an acceptance criterion')
    parser.add_argument('--deep-frames', type=int, default=5)
    parser.add_argument('--source-ip')
    args = parser.parse_args()
    if args.port_aux is not None or args.aux_mode is not None:
        parser.error('SOL2 v3: replace --port-aux/--aux-mode with --port-phase and --port-coherence')
    ports = {S12: args.port_s12, S11: args.port_s11, S22: args.port_s22, PHASE: args.port_phase, COHERENCE: args.port_coherence}
    if not 1 <= args.port_s12 <= 65535 or any(not 0 <= p <= 65535 for p in ports.values()):
        parser.error('S12 port must be 1..65535; optional ports 0..65535')
    active = [p for p in ports.values() if p]
    if len(active) != len(set(active)):
        parser.error('Enabled ports must be distinct')
    if not np.isfinite(args.frequency_mhz) or not START <= args.frequency_mhz <= STOP-STEP:
        parser.error('Frequency must be inside the sampled bin range')
    if not 0 <= args.coherence_threshold <= 1 or not 1 <= args.deep_frames <= HISTORY:
        parser.error('Threshold must be 0..1 and deep-frames 1..300')
    app = QtWidgets.QApplication(sys.argv[:1]); pg.setConfigOptions(antialias=False, imageAxisOrder='row-major')
    receivers = {}
    try:
        for kind, port in ports.items():
            if port:
                receivers[kind] = Receiver(args.bind, port, args.source_ip, expected_type=kind)
        window = FXViewer(receivers, args.mode, args.deep_frames, args.frequency_mhz, args.coherence_threshold)
        for receiver in receivers.values():
            receiver.start()
        signal.signal(signal.SIGINT, lambda *_: window.close())
        signal.signal(signal.SIGTERM, lambda *_: window.close())
        window.show()
        return app.exec_()
    finally:
        for receiver in receivers.values():
            receiver.close()


if __name__ == '__main__':
    sys.exit(main())
