#!/usr/bin/env python3
"""SOL2 FX Interferometer Hybrid Spectrum & Multi-Scale Waterfall Monitor.

Reuses the audited telemetry_protocol.Receiver worker without modification.
Supports:
  - Primary cross-power spectrum |S12| (fast 200 ms frames + deep N-frame integration)
  - Optional simultaneous auto-power overlays S11 and S22 on auxiliary UDP ports
  - Optional auxiliary phase / coherence curve on a dedicated UDP port
  - Frame-indexed zero-roll ring buffers with NaN gap filling and Vietnam Time (UTC+7)
"""
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


class AuxiliaryChannelState:
    """Trạng thái nhận gói tin của các kênh phụ trợ (S11, S22, Coherence/Phase)."""

    def __init__(self, receiver, port):
        self.receiver = receiver
        self.port = port
        self.latest = None
        self.arrived = None
        self.stats = {}
        self.skips = 0
        self.error = None
        self.peak_db = None
        self.peak_freq = None

    def poll(self, freq_axis):
        if self.receiver is None:
            return False
        frames, self.stats, self.skips, self.error = self.receiver.take()
        if not frames:
            return False
        session, frame, stamp, count, values = frames[-1]
        self.latest = (session, frame, stamp, count, values)
        self.arrived = time.monotonic()
        peak_idx = int(np.nanargmax(values))
        self.peak_db = float(values[peak_idx])
        self.peak_freq = float(freq_axis[peak_idx])
        return True


class FXViewer(QtWidgets.QMainWindow):
    """Cửa sổ chính hiển thị phổ giao thoa FX |S12|, lồng phổ S11/S22 và 2 tầng thác nước."""

    def __init__(
        self,
        rx_s12,
        port_s12,
        deep_frames=5,
        rx_s11=None,
        port_s11=None,
        rx_s22=None,
        port_s22=None,
        rx_aux=None,
        port_aux=None,
        aux_mode='coherence',
    ):
        super().__init__()
        self.rx_s12 = rx_s12
        self.port_s12 = port_s12
        self.deep_frames = max(1, int(deep_frames))
        self.aux_mode = aux_mode

        self.ch_s11 = AuxiliaryChannelState(rx_s11, port_s11) if rx_s11 is not None else None
        self.ch_s22 = AuxiliaryChannelState(rx_s22, port_s22) if rx_s22 is not None else None
        self.ch_aux = AuxiliaryChannelState(rx_aux, port_aux) if rx_aux is not None else None

        ports_summary = [f'|S12|:{port_s12}']
        if port_s11:
            ports_summary.append(f'S11:{port_s11}')
        if port_s22:
            ports_summary.append(f'S22:{port_s22}')
        if port_aux:
            ports_summary.append(f'{aux_mode}:{port_aux}')
        self.setWindowTitle(
            f'Solar Correlator — FX Interferometer Viewer ({" | ".join(ports_summary)})'
        )
        self.resize(1480, 960)

        graphics = pg.GraphicsLayoutWidget()
        self.setCentralWidget(graphics)

        self.freq = START + np.arange(BINS) * STEP

        # ====================================================================
        # HÀNG 0: ĐỒ THỊ PHỔ 1D LỒNG GHÉP (|S12|, <|S12|> deep, S11, S22)
        # ====================================================================
        self.spectrum_plot = graphics.addPlot(row=0, col=0)
        self.spectrum_plot.setTitle('FX Cross-Power Spectrum |S12| — Waiting for SOL2 stream...')
        self.spectrum_plot.setLabel('bottom', 'Frequency', units='MHz')
        self.spectrum_plot.setLabel('left', 'Power', units='dB re 1 ADC-count²/bin')
        self.spectrum_plot.getAxis('bottom').enableAutoSIPrefix(False)
        self.spectrum_plot.getAxis('left').enableAutoSIPrefix(False)
        self.spectrum_plot.getAxis('left').setWidth(95)
        self.spectrum_plot.setXRange(START - STEP / 2, STOP - STEP / 2, padding=0)
        self.spectrum_plot.setYRange(10, 95)
        self.spectrum_plot.showGrid(x=True, y=True, alpha=0.22)

        self.legend = self.spectrum_plot.addLegend(offset=(12, 10))

        # Đường S11 và S22
        self.curve_s11 = None
        if self.ch_s11 is not None:
            self.curve_s11 = self.spectrum_plot.plot(
                pen=pg.mkPen('#38bdf8', width=1.0),
                name=f'S11 Auto-Power (:{port_s11})',
            )

        self.curve_s22 = None
        if self.ch_s22 is not None:
            self.curve_s22 = self.spectrum_plot.plot(
                pen=pg.mkPen('#f87171', width=1.0),
                name=f'S22 Auto-Power (:{port_s22})',
            )

        # Đường |S12| tức thời và tích phân sâu
        self.curve_s12_fast = self.spectrum_plot.plot(
            pen=pg.mkPen('#ffdc32', width=1.25),
            name=f'|S12| Fast (:{port_s12})',
        )
        self.curve_s12_deep = None
        if self.deep_frames > 1:
            self.curve_s12_deep = self.spectrum_plot.plot(
                pen=pg.mkPen('#f97316', width=1.8),
                name=f'⟨|S12|⟩ Deep ({self.deep_frames}x frames)',
            )

        self.aux_plot = None
        self.curve_aux = None
        if self.ch_aux is not None:
            self.aux_plot = graphics.addPlot(row=0, col=1)
            self.aux_plot.setLabel('bottom', 'Frequency', units='MHz')
            self.aux_plot.getAxis('bottom').enableAutoSIPrefix(False)
            self.aux_plot.getAxis('left').enableAutoSIPrefix(False)
            self.aux_plot.setXRange(START - STEP / 2, STOP - STEP / 2, padding=0)
            self.aux_plot.showGrid(x=True, y=True, alpha=0.22)
            self.aux_plot.setXLink(self.spectrum_plot)
            if self.aux_mode == 'phase':
                self.aux_plot.setTitle(f'Cross-Phase Δϕ [:{port_aux}]')
                self.aux_plot.setLabel('left', 'Phase', units='rad')
                self.aux_plot.setYRange(-np.pi, np.pi)
                self.curve_aux = self.aux_plot.plot(pen=pg.mkPen('#a78bfa', width=1.1))
            else:
                self.aux_plot.setTitle(f'Coherence γ12 [:{port_aux}]')
                self.aux_plot.setLabel('left', 'Coherence', units='')
                self.aux_plot.setYRange(0.0, 1.05)
                self.curve_aux = self.aux_plot.plot(pen=pg.mkPen('#34d399', width=1.1))

        # ====================================================================
        # HÀNG 1: THÁC NƯỚC TƯƠNG QUAN TỨC THỜI (FAST WATERFALL |S12|)
        # ====================================================================
        self.wf_fast_plot = graphics.addPlot(row=1, col=0)
        self.wf_fast_plot.setTitle('Fast Cross-Power Waterfall |S12| (Single-Frame Resolution)')
        self.wf_fast_plot.setLabel('bottom', 'Frequency', units='MHz')
        self.wf_fast_plot.setLabel('left', 'Fast slots (newest at 0)')
        self.wf_fast_plot.getAxis('bottom').enableAutoSIPrefix(False)
        self.wf_fast_plot.getAxis('left').setWidth(95)
        self.wf_fast_plot.setXRange(START - STEP / 2, STOP - STEP / 2, padding=0)
        self.wf_fast_plot.setYRange(-HISTORY, 0, padding=0)
        self.wf_fast_plot.setXLink(self.spectrum_plot)

        self.buf_fast = WaterfallBuffer(HISTORY, BINS)
        self.img_fast = pg.ImageItem(
            self.buf_fast.display, axisOrder='row-major', levels=(20, 85)
        )
        self.wf_fast_plot.addItem(self.img_fast)
        self.img_fast.setRect(
            QtCore.QRectF(START - STEP / 2, -HISTORY, STOP - START, HISTORY)
        )

        self.cbar_fast = pg.ColorBarItem(
            values=(20, 85),
            colorMap=pg.colormap.get('inferno'),
            label='Fast |S12| (dB)',
            interactive=True,
        )
        self.cbar_fast.setImageItem(self.img_fast)
        graphics.addItem(self.cbar_fast, row=1, col=1)

        # ====================================================================
        # HÀNG 2: THÁC NƯỚC TÍCH PHÂN SÂU (DEEP INTEGRATED WATERFALL ⟨|S12|⟩)
        # ====================================================================
        self.wf_deep_plot = graphics.addPlot(row=2, col=0)
        self.wf_deep_plot.setTitle(
            f'Deep Integrated Waterfall ⟨|S12|⟩ ({self.deep_frames} frames/slot — High Sensitivity)'
        )
        self.wf_deep_plot.setLabel('bottom', 'Frequency', units='MHz')
        self.wf_deep_plot.setLabel('left', 'Deep slots (newest at 0)')
        self.wf_deep_plot.getAxis('bottom').enableAutoSIPrefix(False)
        self.wf_deep_plot.getAxis('left').setWidth(95)
        self.wf_deep_plot.setXRange(START - STEP / 2, STOP - STEP / 2, padding=0)
        self.wf_deep_plot.setYRange(-HISTORY, 0, padding=0)
        self.wf_deep_plot.setXLink(self.spectrum_plot)

        self.integrator = DeepIntegrator(self.deep_frames, BINS)
        self.buf_deep = WaterfallBuffer(HISTORY, BINS)
        self.img_deep = pg.ImageItem(
            self.buf_deep.display, axisOrder='row-major', levels=(20, 85)
        )
        self.wf_deep_plot.addItem(self.img_deep)
        self.img_deep.setRect(
            QtCore.QRectF(START - STEP / 2, -HISTORY, STOP - START, HISTORY)
        )

        self.cbar_deep = pg.ColorBarItem(
            values=(20, 85),
            colorMap=pg.colormap.get('inferno'),
            label='Deep ⟨|S12|⟩ (dB)',
            interactive=True,
        )
        self.cbar_deep.setImageItem(self.img_deep)
        graphics.addItem(self.cbar_deep, row=2, col=1)

        graphics.ci.layout.setRowStretchFactor(0, 3)
        graphics.ci.layout.setRowStretchFactor(1, 3)
        graphics.ci.layout.setRowStretchFactor(2, 3)

        self.latest_s12 = None
        self.latest_deep = None
        self.arrived_s12 = None
        self.peak_s12_db = None
        self.peak_s12_freq = None

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_data)
        self.timer.start(25)

    def update_data(self):
        frames_s12, stats_s12, skips_s12, err_s12 = self.rx_s12.take()
        deep_updated = False

        for session, frame, stamp, count, values in frames_s12:
            self.buf_fast.push_frame(session, frame, values)
            self.latest_s12 = (frame, stamp, count, values)
            self.arrived_s12 = time.monotonic()

            completed_deep = self.integrator.push(session, frame, stamp, count, values)
            for d_sess, d_frame, d_stamp, d_count, d_values in completed_deep:
                self.buf_deep.push_frame(d_sess, d_frame, d_values)
                if not np.all(np.isnan(d_values)):
                    self.latest_deep = (d_frame, d_stamp, d_count, d_values)
                deep_updated = True

        if frames_s12:
            self.img_fast.setImage(self.buf_fast.render_view(), autoLevels=False)
            frame, stamp, count, values = self.latest_s12
            self.curve_s12_fast.setData(self.freq, values)

            peak_idx = int(np.nanargmax(values))
            self.peak_s12_db = float(values[peak_idx])
            self.peak_s12_freq = float(self.freq[peak_idx])

            dt_vn = datetime.fromtimestamp(stamp / 1e6, tz=timezone.utc).astimezone(TZ_VN)
            time_str = dt_vn.strftime('%H:%M:%S.%f')[:-3]
            median_floor = float(np.nanmedian(values))
            snr_db = self.peak_s12_db - median_floor

            self.spectrum_plot.setTitle(
                f'FX |S12| Frame {frame} (M={count}) | '
                f'Peak {self.peak_s12_db:.2f} dB @ {self.peak_s12_freq:.6f} MHz '
                f'(Floor {median_floor:.2f} dB, ΔSNR {snr_db:+.2f} dB) | Giờ VN: {time_str}'
            )

        if deep_updated:
            self.img_deep.setImage(self.buf_deep.render_view(), autoLevels=False)
            if self.latest_deep is not None:
                d_frame, d_stamp, d_count, d_values = self.latest_deep
                if self.curve_s12_deep is not None:
                    self.curve_s12_deep.setData(self.freq, d_values)
                d_peak_idx = int(np.nanargmax(d_values))
                d_peak_db = float(d_values[d_peak_idx])
                d_peak_freq = float(self.freq[d_peak_idx])
                d_floor = float(np.nanmedian(d_values))
                self.wf_deep_plot.setTitle(
                    f'Deep Integrated Waterfall ⟨|S12|⟩ | Slot #{d_frame} (M_total={d_count}) | '
                    f'Peak {d_peak_db:.2f} dB @ {d_peak_freq:.6f} MHz (Floor {d_floor:.2f} dB)'
                )

        if self.ch_s11 is not None and self.ch_s11.poll(self.freq):
            _, _, _, _, val_s11 = self.ch_s11.latest
            self.curve_s11.setData(self.freq, val_s11)

        if self.ch_s22 is not None and self.ch_s22.poll(self.freq):
            _, _, _, _, val_s22 = self.ch_s22.latest
            self.curve_s22.setData(self.freq, val_s22)

        if self.ch_aux is not None and self.ch_aux.poll(self.freq):
            _, a_frame, _, _, val_aux = self.ch_aux.latest
            self.curve_aux.setData(self.freq, val_aux)
            if self.aux_mode == 'phase':
                self.aux_plot.setTitle(f'Cross-Phase Δϕ [:{self.ch_aux.port}] | Frame {a_frame}')
            else:
                max_coh = float(np.nanmax(val_aux))
                self.aux_plot.setTitle(
                    f'Coherence γ12 [:{self.ch_aux.port}] | Frame {a_frame} | Max {max_coh:.3f}'
                )

        age_s12 = (
            'waiting'
            if self.arrived_s12 is None
            else f'{time.monotonic() - self.arrived_s12:.1f}s'
        )
        status_parts = [
            f'|S12|[:{self.port_s12}] ({age_s12}): '
            f'complete={stats_s12.get("complete", 0)} '
            f'gaps={stats_s12.get("frame_gaps", 0)} '
            f'incomplete={stats_s12.get("incomplete", 0)} '
            f'late={stats_s12.get("late", 0)} '
            f'invalid={stats_s12.get("invalid", 0)} '
            f'skips={skips_s12} '
            f'RCVBUF={self.rx_s12.actual_rcvbuf}'
        ]

        if self.ch_s11 is not None:
            status_parts.append(
                f'S11[:{self.ch_s11.port}]: complete={self.ch_s11.stats.get("complete", 0)} '
                f'gaps={self.ch_s11.stats.get("frame_gaps", 0)}'
            )
        if self.ch_s22 is not None:
            status_parts.append(
                f'S22[:{self.ch_s22.port}]: complete={self.ch_s22.stats.get("complete", 0)} '
                f'gaps={self.ch_s22.stats.get("frame_gaps", 0)}'
            )

        if (
            self.ch_s11 is not None
            and self.ch_s22 is not None
            and self.ch_s11.peak_db is not None
            and self.ch_s22.peak_db is not None
        ):
            delta_peak = self.ch_s11.peak_db - self.ch_s22.peak_db
            status_parts.append(f'ΔPeak(S11-S22)={delta_peak:+.2f} dB')
            if self.peak_s12_db is not None:
                coh_db = self.peak_s12_db - 0.5 * (self.ch_s11.peak_db + self.ch_s22.peak_db)
                coh_est = float(np.clip(10.0 ** (coh_db / 10.0), 0.0, 1.0))
                status_parts.append(f'γ12_peak≈{coh_est:.3f}')

        errors = []
        if err_s12:
            errors.append(f'S12 ERR: {err_s12}')
        if self.ch_s11 is not None and self.ch_s11.error:
            errors.append(f'S11 ERR: {self.ch_s11.error}')
        if self.ch_s22 is not None and self.ch_s22.error:
            errors.append(f'S22 ERR: {self.ch_s22.error}')
        if self.ch_aux is not None and self.ch_aux.error:
            errors.append(f'AUX ERR: {self.ch_aux.error}')
        if errors:
            status_parts.append(' | '.join(errors))

        self.statusBar().showMessage('  ||  '.join(status_parts))

    def closeEvent(self, event):
        self.timer.stop()
        self.rx_s12.close()
        if self.ch_s11 is not None and self.ch_s11.receiver is not None:
            self.ch_s11.receiver.close()
        if self.ch_s22 is not None and self.ch_s22.receiver is not None:
            self.ch_s22.receiver.close()
        if self.ch_aux is not None and self.ch_aux.receiver is not None:
            self.ch_aux.receiver.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser(
        description='Solar Correlator — FX Interferometer Hybrid Spectrum & Waterfall Viewer (SOL2)'
    )
    parser.add_argument('--bind', default='0.0.0.0', help='Local bind IPv4 address')
    parser.add_argument(
        '--port',
        '--port-s12',
        dest='port_s12',
        type=int,
        default=9999,
        help='Primary UDP port for Cross-Power Spectrum |S12| (default: 9999)',
    )
    parser.add_argument(
        '--port-s11',
        type=int,
        default=0,
        help='Optional UDP port for SDR 1 Auto-Power S11 overlay (0 = disabled)',
    )
    parser.add_argument(
        '--port-s22',
        type=int,
        default=0,
        help='Optional UDP port for SDR 2 Auto-Power S22 overlay (0 = disabled)',
    )
    parser.add_argument(
        '--port-aux',
        type=int,
        default=0,
        help='Optional UDP port for Coherence or Phase stream (0 = disabled)',
    )
    parser.add_argument(
        '--aux-mode',
        choices=('coherence', 'phase'),
        default='coherence',
        help='Interpretation of --port-aux stream (coherence in [0,1] or phase in [-pi,pi])',
    )
    parser.add_argument(
        '--deep-frames',
        type=int,
        default=5,
        help='Number of fast frames averaged per Deep Waterfall slot (default: 5 => ~1.0s)',
    )
    parser.add_argument('--source-ip', help='Accept datagrams only from this sender IPv4')
    args = parser.parse_args()

    active_ports = [p for p in (args.port_s12, args.port_s11, args.port_s22, args.port_aux) if p > 0]
    if len(active_ports) != len(set(active_ports)):
        parser.error('All enabled UDP ports (--port-s12, --port-s11, --port-s22, --port-aux) must be distinct.')

    app = QtWidgets.QApplication(sys.argv[:1])
    pg.setConfigOptions(antialias=False, imageAxisOrder='row-major')

    rx_s12 = Receiver(args.bind, args.port_s12, args.source_ip)
    rx_s11 = Receiver(args.bind, args.port_s11, args.source_ip) if args.port_s11 > 0 else None
    rx_s22 = Receiver(args.bind, args.port_s22, args.source_ip) if args.port_s22 > 0 else None
    rx_aux = Receiver(args.bind, args.port_aux, args.source_ip) if args.port_aux > 0 else None

    all_receivers = [r for r in (rx_s12, rx_s11, rx_s22, rx_aux) if r is not None]

    try:
        window = FXViewer(
            rx_s12=rx_s12,
            port_s12=args.port_s12,
            deep_frames=args.deep_frames,
            rx_s11=rx_s11,
            port_s11=args.port_s11 if args.port_s11 > 0 else None,
            rx_s22=rx_s22,
            port_s22=args.port_s22 if args.port_s22 > 0 else None,
            rx_aux=rx_aux,
            port_aux=args.port_aux if args.port_aux > 0 else None,
            aux_mode=args.aux_mode,
        )
        for r in all_receivers:
            r.start()
        signal.signal(signal.SIGINT, lambda *_: window.close())
        signal.signal(signal.SIGTERM, lambda *_: window.close())
        window.show()
        return app.exec_()
    finally:
        for r in all_receivers:
            r.close()


if __name__ == '__main__':
    sys.exit(main())
