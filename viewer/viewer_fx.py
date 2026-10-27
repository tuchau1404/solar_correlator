#!/usr/bin/env python3
"""SOL2 v3 CW / solar display, bounded frame matching and phase quality masking.

Legacy v2 remains in viewer.py / viewer_dual.py and is rejected here.
"""
import argparse
import colorsys
import json
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import signal
import sys
import time
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets
import viewer_ui as ui
from telemetry_protocol import Receiver, BINS, FrameJoiner, S12, S11, S22, PHASE, COHERENCE
from science_model import ObservationHistory, PERIOD, masked_phase

HISTORY = 300  # Deep waterfall slots; independent of fast/phase retention.
WF_HISTORY_SECONDS = 1800
WF_HISTORY_ROWS = int(np.ceil(WF_HISTORY_SECONDS / PERIOD)) + 2
START, STOP = 30.0, 40.0
STEP = (STOP - START) / BINS

# Múi giờ Việt Nam: UTC+7
TZ_VN = timezone(timedelta(hours=7))


def nearest_frequency_bin(frequency):
    """Half-up quantization, identical to ObservationHistory.select."""
    return int(np.clip(np.floor((frequency - START) / STEP + .5), 0, BINS - 1))


def format_vn_time(seconds, fmt='%H:%M:%S', *, milliseconds=False, invalid='invalid UTC'):
    """Calendar rendering is partial: SOL2 uint64 timestamps exceed datetime's range."""
    try:
        if not np.isfinite(seconds):
            return invalid
        text = datetime.fromtimestamp(seconds, TZ_VN).strftime(fmt)
        return text[:-3] if milliseconds else text
    except (ValueError, OverflowError, OSError):
        return invalid


def coherence_time_series(trace):
    """Coherence continuity depends on frame/time identity, not phase quality/wrap."""
    x, gamma = [], []
    previous = None
    for row in trace:
        if row is None:
            x.append(np.nan); gamma.append(np.nan)
            previous = None
            continue
        session, fid, stamp, count, phase, value = row
        if previous is not None:
            ps, pf, pt = previous
            if (session != ps or fid != pf + 1 or stamp <= pt
                    or stamp - pt > PERIOD * 1.5e6):
                x.append(np.nan); gamma.append(np.nan)
        x.append(stamp / 1e6)
        gamma.append(value if np.isfinite(value) and 0 <= value <= 1 else np.nan)
        previous = (session, fid, stamp)
    return np.asarray(x), np.asarray(gamma)


def reconstruct_fringe(db, phase, gamma, threshold=.5, normalized=True):
    """Recover post-integration correlation components; inputs are never modified.

    Linear magnitude follows the sender's 10*log10(abs(S12)) convention.
    At/below its -120 dB floor the original linear magnitude is unknowable.
    Normalized components use gamma instead and do not invert that floor.
    """
    db, phase, gamma = (np.asarray(a, dtype=np.float64) for a in (db, phase, gamma))
    if db.shape != phase.shape or db.shape != gamma.shape:
        raise ValueError('Fringe inputs must have identical shapes')
    if not np.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError('Coherence threshold must be finite and within [0, 1]')
    good = (np.isfinite(db) & np.isfinite(phase) & np.isfinite(gamma)
            & (np.abs(phase) <= np.pi + 1e-6) & (gamma > 0)
            & (gamma <= 1) & (gamma >= threshold))
    if normalized:
        amplitude = gamma
    else:
        with np.errstate(over='ignore', under='ignore', invalid='ignore'):
            amplitude = np.power(10., db / 10.)
        good &= (db > -120.) & np.isfinite(amplitude) & (amplitude > 0)
    real, imag = np.full(db.shape, np.nan), np.full(db.shape, np.nan)
    real[good] = amplitude[good] * np.cos(phase[good])
    imag[good] = amplitude[good] * np.sin(phase[good])
    return real, imag


def build_fringe_series(rows, trace, bin_index, threshold=.5, normalized=True):
    """Join magnitude to scalar trace by full observation identity, never by index."""
    magnitudes = {}
    for row in rows:
        if row is None:
            continue
        values = row[4].get(S12)
        if values is not None and 0 <= bin_index < len(values):
            magnitudes[tuple(row[:4])] = float(values[bin_index])
    x, db, phase, gamma, keys = [], [], [], [], []
    previous = None

    def gap():
        x.append(np.nan); db.append(np.nan); phase.append(np.nan)
        gamma.append(np.nan); keys.append(None)

    for row in trace:
        if row is None:
            gap()
            previous = None
            continue
        session, fid, stamp, count, raw_phase, coherence = row
        if previous is not None:
            ps, pf, pt = previous
            if (session != ps or fid != pf + 1 or stamp <= pt
                    or stamp - pt > PERIOD * 1.5e6):
                gap()
        key = tuple(row[:4])
        x.append(stamp / 1e6)
        db.append(magnitudes.get(key, np.nan))
        phase.append(raw_phase); gamma.append(coherence); keys.append(key)
        previous = (session, fid, stamp)
    real, imag = reconstruct_fringe(db, phase, gamma, threshold, normalized)
    lookup = {key: (db[i], float(real[i]), float(imag[i]))
              for i, key in enumerate(keys) if key is not None}
    return np.asarray(x), real, imag, lookup


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
        self.total_m = 0
        self.last_stamp = None
        self.deep_frame_id = 0
        self.last_session = None
        self.last_frame_id = None

    def reset(self):
        self.accum_linear.fill(0.0)
        self.count = 0
        self.total_m = 0
        self.last_stamp = None
        self.last_frame_id = None

    def time_break(self, stamp, m_frames):
        expected_us = max(1, m_frames) * 1024 / 10e6 * 1e6
        return self.last_stamp is not None and (stamp <= self.last_stamp or stamp-self.last_stamp > expected_us*1.5)

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
            self.total_m = 0
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

        if self.time_break(stamp, m_frames):
            self.accum_linear.fill(0.)
            self.count = 0
            self.total_m = 0
        self.last_stamp = stamp
        self.last_frame_id = frame_id
        linear_power = np.power(10.0, np.asarray(db_values, dtype=np.float64) * 0.1)
        self.accum_linear += linear_power
        self.count += 1
        self.total_m += m_frames

        if self.count >= self.group_size:
            mean_linear = np.maximum(self.accum_linear / float(self.count), 1e-12)
            deep_db = (10.0 * np.log10(mean_linear)).astype(np.float32)
            self.deep_frame_id += 1
            completed.append(
                (
                    session,
                    self.deep_frame_id,
                    stamp,
                    self.total_m,
                    deep_db,
                )
            )
            self.accum_linear.fill(0.0)
            self.count = 0
            self.total_m = 0

        return completed


class VietnamTimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [format_vn_time(v, '%H:%M:%S', invalid='')
                if np.isfinite(v) and v > 0 else '' for v in values]


class SlotTimeAxis(pg.AxisItem):
    """Frame slots can be irregular in UTC; label actual received timestamps."""
    def __init__(self, model, **kwargs):
        super().__init__(**kwargs)
        self.model = model
        self.display_rows = ()
        self.display_end = HISTORY

    def tickStrings(self, values, scale, spacing):
        rows = self.display_rows
        labels = []
        for v in values:
            if not np.isfinite(v):
                labels.append('')
                continue
            i = int(np.floor(v)) - (self.display_end - len(rows))
            row = rows[i] if 0 <= i < len(rows) else None
            labels.append(format_vn_time(row[2] / 1e6, '%H:%M:%S', invalid='') if row else '')
        return labels


class FXViewer(QtWidgets.QMainWindow):
    def _is_h5(self):
        return hasattr(self, 'h5') and self.h5.active

    def _open_h5_offline(self):
        self.h5.choose_files()

    def start_live_receivers(self):
        if not self.receivers and self._receiver_factory is not None:
            receivers = self._receiver_factory()
            self.receivers = receivers
            self.joiner = FrameJoiner(enabled=receivers)

    def __init__(self, receivers, mode='cw', deep_frames=5, frequency=35.5, threshold=.5, window_seconds=120,
                 *, settings_store=None, persist_settings=True, receiver_factory=None):
        super().__init__()
        offline_action = self.menuBar().addMenu('File').addAction('Open H5 files…')
        offline_action.triggered.connect(self._open_h5_offline)
        self._persist_settings = persist_settings
        self.settings = settings_store if settings_store is not None else QtCore.QSettings('SolarCorrelator', 'FXViewer')
        self._paused_since = None
        self.receivers = receivers
        self._receiver_factory = receiver_factory
        self.deep_frames = max(1, int(deep_frames))
        self.model = ObservationHistory(WF_HISTORY_ROWS, frequency, trace_history=int(np.ceil(1800 / PERIOD)) + 2)
        self.joiner = FrameJoiner(enabled=receivers)
        self.integrator = DeepIntegrator(self.deep_frames, BINS)
        self.buf_deep = WaterfallBuffer(HISTORY, BINS)  # Compatibility buffer; rendering uses timed groups.
        self.deep_history = deque(maxlen=int(np.ceil(WF_HISTORY_ROWS / self.deep_frames)) + 2)
        self._display_deep_rows = ()
        self._deep_group_start = None
        self._deep_source_session = None
        self._power_levels_syncing = False
        self.freq = START + np.arange(BINS) * STEP

        # Bộ nhớ đệm dữ liệu đường cong 1D mới nhất (phục vụ Pause Display, Hover, Peak & Auto-scale Y)
        self.latest_streams = {
            S12: np.full(BINS, np.nan, dtype=np.float32),
            S11: np.full(BINS, np.nan, dtype=np.float32),
            S22: np.full(BINS, np.nan, dtype=np.float32),
        }
        self.latest_deep_db = np.full(BINS, np.nan, dtype=np.float32)
        self.displayed_streams = {
            S12: np.full(BINS, np.nan, dtype=np.float32),
            S11: np.full(BINS, np.nan, dtype=np.float32),
            S22: np.full(BINS, np.nan, dtype=np.float32),
        }
        self.displayed_deep_db = np.full(BINS, np.nan, dtype=np.float32)

        self._updating_spectrum_y = False
        self._updating_time_x = False
        self._display_rows = ()
        self._display_trace = ()
        self._layout_pending = False
        self._wf_display_end = HISTORY
        self._wf_observation_key = None
        self._wf_view_range = (0., float(HISTORY))
        self._wf_syncing = False
        self._frequency_syncing = False
        self._time_ranges_syncing = False
        self._fringe_values_by_key = {}
        self._wf_render_pending = False
        self._wf_image_signature = None

        # Trạng thái thước đo độ dốc trôi tần 2 điểm (Drift Rate Tool)
        self.drift_plot_key = None
        self.drift_points = []
        self.drift_summary_text = 'Drift Measure: Shift+Click 2 points on any Waterfall to measure Δf/Δt'
        self.peak_summary_text = 'Peak |S12|: waiting for spectrum data'

        self.setWindowTitle('SOLAR / Correlator — SOL2 v3')
        self.resize(1560, 1080)
        self._last_observation_key = None
        self._last_observation_received = None
        self._focus_key = None
        self._theme_window()

        root, layout, self.content_scroll = ui.create_root(self)
        self.connection_badge = ui.brand_header(layout)
        self.connection_badge.setToolTip('Status reflects matched observations, not a TCP-style connection.')

        toolbar, toolbar_row = ui.toolbar_panel(layout)
        self.sidebar_button = QtWidgets.QPushButton('Controls')
        self.sidebar_button.setCheckable(True)
        self.sidebar_button.setChecked(True)
        self.sidebar_button.setToolTip('Show or hide observation controls')
        toolbar_row.addWidget(self.sidebar_button)
        toolbar_row.addWidget(QtWidgets.QLabel('Workspace'))
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(['CW', 'Solar'])
        self.mode.setCurrentText('Solar' if mode == 'solar' else 'CW')
        self.mode.setMinimumWidth(90)
        toolbar_row.addWidget(self.mode)
        self.display_badge = QtWidgets.QLabel('DISPLAY LIVE')
        self.display_badge.setObjectName('displayBadge')
        toolbar_row.addWidget(self.display_badge)
        self.data_age_label = QtWidgets.QLabel('Last matched: —')
        self.data_age_label.setObjectName('mutedLabel')
        self.data_age_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.data_age_label.setMinimumWidth(95)
        self.data_age_label.setToolTip('Local elapsed time since the newest matched observation was processed; not capture age or network latency.')
        toolbar_row.addWidget(self.data_age_label, 1)
        toolbar_row.addStretch()
        self.return_live_button = QtWidgets.QPushButton('Return to live')
        self.return_live_button.setToolTip('L: resume display and follow the latest timestamp; keep the current layout')
        self.return_live_button.setEnabled(False)
        toolbar_row.addWidget(self.return_live_button)
        self.pause_button = QtWidgets.QPushButton('Pause')
        self.pause_button.setObjectName('pauseButton')
        self.pause_button.setCheckable(True)
        self.pause_button.setToolTip('Space: freeze plots and readouts; UDP reception and integration continue')
        toolbar_row.addWidget(self.pause_button)
        self.help_button = QtWidgets.QPushButton('Shortcuts')
        self.help_button.setToolTip('Keyboard controls and measurement guide')
        toolbar_row.addWidget(self.help_button)

        self.splitter = ui.create_splitter(layout)
        self.sidebar, side = ui.create_sidebar(self.splitter)

        def section(title):
            return ui.sidebar_section(side, title)

        def field(title, widget):
            return ui.sidebar_field(side, title, widget)

        section('OBSERVATION')
        self.frequency = QtWidgets.QDoubleSpinBox()
        self.frequency.setDecimals(6)
        self.frequency.setRange(START, STOP - STEP)
        self.frequency.setSingleStep(STEP)
        self.frequency.setSuffix(' MHz')
        self.frequency.setValue(frequency)
        field('Requested frequency', self.frequency)
        self.bin_label = QtWidgets.QLabel()
        self.bin_label.setObjectName('mutedLabel')
        self.bin_label.setWordWrap(True)
        side.addWidget(self.bin_label)
        bin_steps = QtWidgets.QHBoxLayout()
        self.bin_minus_button = QtWidgets.QPushButton('−1 bin')
        self.bin_plus_button = QtWidgets.QPushButton('+1 bin')
        self.bin_minus_button.setToolTip('Move one sampled frequency bin left')
        self.bin_plus_button.setToolTip('Move one sampled frequency bin right')
        bin_steps.addWidget(self.bin_minus_button)
        bin_steps.addWidget(self.bin_plus_button)
        side.addLayout(bin_steps)
        self.snap_peak_button = QtWidgets.QPushButton('Snap to peak |S12|')
        self.snap_peak_button.setToolTip('P: select the strongest finite |S12| bin in the displayed spectrum')
        side.addWidget(self.snap_peak_button)
        self.threshold = QtWidgets.QDoubleSpinBox()
        self.threshold.setRange(0, 1)
        self.threshold.setSingleStep(.05)
        self.threshold.setValue(threshold)
        field('Phase display mask: minimum γ', self.threshold)
        note = QtWidgets.QLabel('Display mask only · γ = 0 is invalid')
        note.setObjectName('mutedLabel')
        note.setWordWrap(True)
        side.addWidget(note)

        section('TIME TRACE')
        self.time_window = QtWidgets.QSpinBox()
        self.time_window.setRange(15, 1800)
        self.time_window.setValue(window_seconds)
        self.time_window.setSingleStep(15)
        self.time_window.setSuffix(' s')
        field('Time window (all traces)', self.time_window)
        self.follow_live = QtWidgets.QCheckBox('Follow latest timestamp')
        self.follow_live.setChecked(True)
        side.addWidget(self.follow_live)
        self.reset_time_button = QtWidgets.QPushButton('Reset time view')
        side.addWidget(self.reset_time_button)

        section('FRINGES / SELECTED BIN')
        self.fringe_mode = QtWidgets.QComboBox()
        self.fringe_mode.addItem('Normalized (γ)', 'normalized')
        self.fringe_mode.addItem('Linear Re / Im', 'linear')
        self.fringe_mode.setToolTip('Normalized: γ cosφ and γ sinφ. Linear: 10^(S12_dB/10) times cosφ and sinφ; ADC-count²/bin, not dBm.')
        field('Fringe amplitude', self.fringe_mode)
        self.fringe_re_check = QtWidgets.QCheckBox('Re(S12) / real')
        self.fringe_im_check = QtWidgets.QCheckBox('Im(S12) / imaginary')
        self.fringe_auto_y = QtWidgets.QCheckBox('Auto-scale fringe Y')
        for cb in (self.fringe_re_check, self.fringe_im_check, self.fringe_auto_y):
            cb.setChecked(True)
            side.addWidget(cb)
        note = QtWidgets.QLabel('Correlation components, not raw SDR I/Q. Same-frame data; γ mask applies. Phase as received; backend calibration may apply.')
        note.setWordWrap(True)
        note.setObjectName('mutedLabel')
        side.addWidget(note)

        section('WATERFALL HISTORY')
        self.wf_window = QtWidgets.QSpinBox()
        self.wf_window.setRange(15, WF_HISTORY_SECONDS)
        self.wf_window.setValue(60)
        self.wf_window.setSingleStep(15)
        self.wf_window.setSuffix(' s')
        self.wf_window.setToolTip('Frame-slot window at nominal cadence. UTC ticks use actual frame timestamps; irregular timestamps are not uniformly spaced in elapsed seconds.')
        field('Waterfall window (nominal)', self.wf_window)
        self.wf_follow = QtWidgets.QCheckBox('Follow latest waterfall')
        self.wf_follow.setChecked(True)
        side.addWidget(self.wf_follow)
        self.link_power_scales = QtWidgets.QCheckBox('Link Fast / Deep dB scale')
        self.link_power_scales.setChecked(True)
        side.addWidget(self.link_power_scales)
        self.wf_history_label = QtWidgets.QLabel('History: waiting · retains up to 30 min in RAM')
        self.wf_history_label.setObjectName('mutedLabel')
        self.wf_history_label.setWordWrap(True)
        side.addWidget(self.wf_history_label)

        section('SPECTRUM CURVES')
        self.curve_checks = {}
        for key, label in [(S12, '|S12|'), ('DEEP', 'Mean |S12|'), (S11, 'S11'), (S22, 'S22')]:
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(True)
            self.curve_checks[key] = cb
            side.addWidget(cb)
        self.autoscale_y_check = QtWidgets.QCheckBox('Auto-scale spectrum Y')
        self.autoscale_y_check.setChecked(True)
        side.addWidget(self.autoscale_y_check)
        self.fit_y_button = QtWidgets.QPushButton('Fit spectrum Y')
        side.addWidget(self.fit_y_button)

        section('VISIBLE PLOTS')
        self.plot_checks = {}
        self._plot_names = {
            'power': 'S11 / S22 / |S12| power trace', 'spectrum': 'Spectrum', 'phase': 'Phase trace', 'fringe': 'Fringes Re / Im', 'gamma': 'Coherence trace',
            'wf_fast': 'Fast waterfall', 'wf_phase': 'Phase waterfall', 'wf_deep': 'Deep waterfall',
        }
        for key, label in self._plot_names.items():
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(True)
            self.plot_checks[key] = cb
            side.addWidget(cb)
        self.power_checks={}
        for key,label in ((S11,'Power trace S11'),(S22,'Power trace S22'),(S12,'Power trace |S12|')):
            cb=QtWidgets.QCheckBox(label);cb.setChecked(True)
            cb.toggled.connect(lambda *_: self._update_power_plot())
            self.power_checks[key]=cb;side.addWidget(cb)
        self.clear_measure_button = QtWidgets.QPushButton('Clear drift measurement')
        self.clear_measure_button.setToolTip('Esc: clear the measurement. Space: resume after measuring.')
        side.addWidget(self.clear_measure_button)
        side.addStretch()
        footer = QtWidgets.QLabel('30–40 MHz  /  2048 bins\nUTC+07:00 · Vietnam')
        footer.setObjectName('mutedLabel')
        side.addWidget(footer)

        workspace, right = ui.create_workspace(self.splitter)
        self.workspace_layout = right
        self.metric_values, self.metric_details = ui.metric_cards(right, [
            ('frequency', 'SELECTED BIN'), ('phase', 'RECEIVED PHASE'),
            ('gamma', 'COHERENCE  γ'), ('time', 'FRAME TIME  /  VN')])

        heading = QtWidgets.QHBoxLayout()
        self.workspace_title = QtWidgets.QLabel('CW OBSERVATION')
        self.workspace_title.setObjectName('workspaceTitle')
        heading.addWidget(self.workspace_title)
        heading.addStretch()
        heading.addWidget(QtWidgets.QLabel('Focus'))
        self.focus_combo = QtWidgets.QComboBox()
        self.focus_combo.addItem('Overview', None)
        for key, name in self._plot_names.items():
            self.focus_combo.addItem(name, key)
        self.focus_combo.setMinimumWidth(160)
        self.focus_combo.setToolTip('Enlarge one plot; Overview restores your visibility choices')
        heading.addWidget(self.focus_combo)
        right.addLayout(heading)

        self.graphics = g = pg.GraphicsLayoutWidget()
        g.setBackground('#0b121c')
        g.setObjectName('graphicsPanel')
        g.viewport().installEventFilter(self)
        g.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        g.ci.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        right.addWidget(g, 1)
        self.empty_label = QtWidgets.QLabel('No plots selected. Enable a plot in Controls or choose a Focus view.')
        self.empty_label.setObjectName('emptyLabel')
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_label.hide()
        right.addWidget(self.empty_label)

        self.readout_frame = QtWidgets.QFrame()
        self.readout_frame.setObjectName('readoutPanel')
        readout_layout = QtWidgets.QVBoxLayout(self.readout_frame)
        readout_layout.setContentsMargins(12, 8, 12, 8)
        readout_layout.setSpacing(4)
        mono_font = QtGui.QFont('DejaVu Sans Mono', 9)
        mono_font.setStyleHint(QtGui.QFont.TypeWriter)
        self.readout_pinned = QtWidgets.QLabel('PINNED: waiting for selected-bin data')
        self.readout_hover = QtWidgets.QLabel('HOVER: inspect a plot · Shift+Click twice on a waterfall to measure drift')
        self.readout_metrics = QtWidgets.QLabel(f'{self.peak_summary_text} || {self.drift_summary_text}')
        for label, color in [(self.readout_pinned, '#c3d4e6'), (self.readout_hover, '#8ab8dd'),
                             (self.readout_metrics, '#d1b77f')]:
            label.setFont(mono_font)
            label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
            label.setMinimumWidth(0)
            label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            label.setStyleSheet(f'color: {color}; background: transparent;')
            readout_layout.addWidget(label)
        self.readout = self.readout_pinned
        right.addWidget(self.readout_frame)
        self.statusBar().setSizeGripEnabled(True)

        # Cấu hình co giãn 2 cột trong GraphicsLayout: Cột 0 (đồ thị) co giãn 100%, Cột 1 (Colorbar) cố định
        g.ci.layout.setColumnStretchFactor(0, 1)
        g.ci.layout.setColumnStretchFactor(1, 0)

        # Hàng 0: Đồ thị Phổ 1D (S11 / S22 / |S12| / Mean |S12|)
        self.spectrum_plot = g.addPlot(row=0, col=0, colspan=2, title='Matched S11 / S22 / |S12| spectrum')
        self.spectrum_plot.setLabel('bottom', 'Frequency', units='MHz')
        self.spectrum_plot.setLabel('left', 'Power', units='dB')
        self.spectrum_plot.setTitle('Matched S11 / S22 / |S12| — dB re 1 ADC-count²/bin')
        self.spectrum_plot.setXRange(START - STEP / 2, STOP - STEP / 2, padding=0)
        self.spectrum_plot.addLegend(colCount=4, offset=(10, 8), labelTextSize='9pt')
        self.curves = {
            kind: self.spectrum_plot.plot(pen=pg.mkPen(color, width=1), name=name)
            for kind, color, name in [
                (S12, '#ffdc32', '|S12|'),
                (S11, '#38bdf8', 'S11'),
                (S22, '#f87171', 'S22'),
            ]
        }
        self.deep_curve = self.spectrum_plot.plot(
            pen=pg.mkPen('#f97316', width=1.5), name='Mean |S12| (linear power average)'
        )
        self.marker = pg.InfiniteLine(pos=self.model.actual, angle=90, movable=True,
                                      bounds=(START, STOP - STEP),
                                      pen=pg.mkPen('#a78bfa', width=1.5),
                                      hoverPen=pg.mkPen('#e1c5ff', width=3))
        self.marker.setToolTip('Drag and release to select the nearest frequency bin')
        self.spectrum_plot.addItem(self.marker)

        # Điểm đánh dấu đỉnh phổ |S12| (Peak Marker)
        self.peak_marker = pg.ScatterPlotItem(
            size=11,
            symbol='t',
            pen=pg.mkPen('#0f172a', width=1.2),
            brush=pg.mkBrush('#facc15'),
        )
        self.spectrum_plot.addItem(self.peak_marker)

        # Hàng 1: Đồ thị Pha theo thời gian Phase(t)
        self.phase_plot = g.addPlot(
            row=1, col=0, colspan=2, axisItems={'bottom': VietnamTimeAxis(orientation='bottom')}
        )
        self.phase_plot.setLabel('bottom', 'Received UTC timestamp → Vietnam UTC+07:00')
        self.phase_plot.setLabel('left', 'Phase', units='°')
        self.phase_plot.setYRange(-180, 180, padding=0)
        self.phase_curve = self.phase_plot.plot(pen=pg.mkPen('#a78bfa', width=1.5), connect='finite')
        self.phase_points = pg.ScatterPlotItem(size=5, pen=None, brush='#a78bfa')
        self.phase_plot.addItem(self.phase_points)

        # Hàng 2: Đồ thị Độ kết hợp γ(t)
        self.gamma_plot = g.addPlot(
            row=3, col=0, colspan=2, axisItems={'bottom': VietnamTimeAxis(orientation='bottom')}
        )
        self.gamma_plot.setLabel('left', 'γ amplitude')
        self.gamma_plot.setLabel('bottom', 'Vietnam UTC+07:00')
        self.gamma_plot.setYRange(0, 1.05)
        self.gamma_curve = self.gamma_plot.plot(pen='#34d399', connect='finite')
        self.mask_line = pg.InfiniteLine(pos=threshold, angle=0, pen='#ef4444')
        self.gamma_plot.addItem(self.mask_line)

        self.fringe_plot = g.addPlot(row=2, col=0, colspan=2,
            title='Selected-bin complex correlation fringes',
            axisItems={'bottom': VietnamTimeAxis(orientation='bottom')})
        self.fringe_plot.setLabel('bottom', 'Vietnam time (UTC+07:00)')
        self.fringe_plot.setLabel('left', 'Normalized R / I')
        self.fringe_plot.setYRange(-1.05, 1.05, padding=0)
        self.fringe_plot.setMouseEnabled(x=True, y=True)
        self.fringe_plot.addLegend(colCount=2, offset=(10, 8), labelTextSize='9pt')
        self.fringe_re_curve = self.fringe_plot.plot(pen=pg.mkPen('#6ed8e4', width=1.5),
            name='Real component', connect='finite')
        self.fringe_im_curve = self.fringe_plot.plot(pen=pg.mkPen('#f1ad6a', width=1.5),
            name='Imaginary component', connect='finite')
        self.fringe_zero = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen('#3a4b5e'))
        self.fringe_plot.addItem(self.fringe_zero, ignoreBounds=True)
        self.fringe_plot.getAxis('left').enableAutoSIPrefix(False)

        # Hàng 3 & 4: Waterfall |S12| nhanh và Waterfall Pha
        self.wf_fast_plot, self.img_fast, self.cbar_fast = self.make_waterfall(4, '|S12| dB', False)
        self.wf_phase_plot, self.img_phase, self.cbar_phase = self.make_waterfall(
            5, 'Phase ° — masked pixels transparent', True
        )

        # Hàng 5: Waterfall Tích phân sâu Mean |S12|
        self.wf_deep_plot = g.addPlot(
            row=6, col=0, axisItems={'left': SlotTimeAxis(self.model, orientation='left')}, title=f'Mean |S12|, {self.deep_frames} frames/slot (not coherent integration)'
        )
        self.wf_deep_plot.setLabel('left', 'VN time')
        self.wf_deep_plot.setLabel('bottom', 'Frequency', units='MHz')
        self.wf_deep_plot.setXRange(START-STEP/2, STOP-STEP/2, padding=0)
        self.wf_deep_plot.setMouseEnabled(x=True, y=True)
        self.wf_deep_plot.enableAutoRange(x=False, y=False)
        self.wf_deep_plot.setYRange(0, HISTORY, padding=0)
        self.img_deep = pg.ImageItem(self.buf_deep.display, axisOrder='row-major', levels=(0, 1))
        self.wf_deep_plot.addItem(self.img_deep)
        self.img_deep.setRect(QtCore.QRectF(START - STEP / 2, 0, STOP - START, HISTORY))
        self.cbar_deep = pg.ColorBarItem(
            values=(-60, 85), colorMap=pg.colormap.get('inferno'), label='Mean |S12| dB', interactive=True
        )
        self.cbar_deep.setImageItem(self.img_deep)
        g.addItem(self.cbar_deep, row=6, col=1)

        self.power_plot=g.addPlot(row=7,col=0,colspan=2,
            axisItems={'bottom':VietnamTimeAxis(orientation='bottom')})
        self.power_plot.setLabel('left','Power [dB re 1 ADC-count²/bin]')
        self.power_plot.setLabel('bottom','Vietnam time (UTC+07:00)')
        self.power_plot.addLegend()
        self.power_curves={key:self.power_plot.plot(name=name,pen=pg.mkPen(color,width=1.5),connect='finite')
                           for key,name,color in ((S11,'S11','#38bdf8'),(S22,'S22','#f87171'),(S12,'|S12|','#ffdc32'))}
        self.power_plot.enableAutoRange(x=False,y=True)
        # Căn lề trục tung đồng nhất 110px và tắt SI prefix cho toàn bộ đồ thị
        self.all_plots = (
            self.spectrum_plot,
            self.power_plot,
            self.phase_plot,
            self.fringe_plot,
            self.gamma_plot,
            self.wf_fast_plot,
            self.wf_phase_plot,
            self.wf_deep_plot,
        )
        for plot in self.all_plots:
            plot.getAxis('left').setWidth(92)
            plot.getAxis('bottom').enableAutoSIPrefix(False)

        self.spectrum_plot.setXRange(START - STEP / 2, STOP - STEP / 2, padding=0)
        self.phase_plot.setMouseEnabled(x=True, y=False)
        self.gamma_plot.setMouseEnabled(x=True, y=False)

        self._theme_plots()

        # Khởi tạo hệ thống con trỏ chữ thập đồng bộ (Crosshairs), Tooltip & Thước đo Drift
        self._init_crosshairs_and_overlays()

        # Kết nối sự kiện tương tác
        self.graphics.scene().sigMouseClicked.connect(self.scene_mouse_clicked)
        self.graphics.scene().sigMouseMoved.connect(self.scene_mouse_moved)
        for plot in self.time_plots.values():
            plot.vb.sigXRangeChanged.connect(lambda vb, ranges, plot=plot: self._sync_trace_time_ranges(plot, ranges))
        self._sync_trace_time_ranges(self.phase_plot, self.phase_plot.vb.viewRange()[0])
        self.fringe_plot.vb.sigRangeChangedManually.connect(self.pause_follow)
        self.fringe_plot.vb.sigRangeChangedManually.connect(self._on_fringe_range_manually)
        self.fringe_mode.currentIndexChanged.connect(self._on_fringe_mode_changed)
        for cb in (self.fringe_re_check, self.fringe_im_check, self.fringe_auto_y):
            cb.toggled.connect(self._update_fringe_plot)
        self.phase_plot.vb.sigRangeChangedManually.connect(self.pause_follow)
        self.gamma_plot.vb.sigRangeChangedManually.connect(self.pause_follow)
        self.spectrum_plot.vb.sigRangeChangedManually.connect(self.on_spectrum_range_changed_manually)

        self.time_window.valueChanged.connect(self.update_time_view)
        self.follow_live.toggled.connect(self.update_time_view)
        self.reset_time_button.clicked.connect(self.reset_time_view)
        self.frequency.valueChanged.connect(self.select_frequency)
        self.threshold.valueChanged.connect(self.render)
        self.mode.currentTextChanged.connect(self.apply_mode)

        self.pause_button.toggled.connect(self.on_pause_toggled)
        self.snap_peak_button.clicked.connect(self.snap_to_peak_s12)
        self.clear_measure_button.clicked.connect(self.clear_drift_measurement)
        self.autoscale_y_check.toggled.connect(self.on_autoscale_y_toggled)
        self.fit_y_button.clicked.connect(self.fit_spectrum_y)

        for key, cb in self.curve_checks.items():
            cb.toggled.connect(self.update_curve_visibility)
        for key, cb in self.plot_checks.items():
            cb.toggled.connect(self.update_layout_geometry)

        self.sidebar_button.toggled.connect(self.sidebar.setVisible)
        self.sidebar_button.toggled.connect(self._schedule_layout_sync)
        self.focus_combo.currentIndexChanged.connect(self._change_focus)
        self.splitter.splitterMoved.connect(self._schedule_layout_sync)
        self.help_button.clicked.connect(self._show_shortcuts)
        for plot in (self.spectrum_plot, self.wf_fast_plot, self.wf_phase_plot, self.wf_deep_plot):
            plot.vb.sigXRangeChanged.connect(lambda vb, ranges, plot=plot: self._sync_frequency_ranges(plot, ranges))
        self.cbar_fast.sigLevelsChanged.connect(lambda *_: self._link_power_from(self.cbar_fast))
        self.cbar_deep.sigLevelsChanged.connect(lambda *_: self._link_power_from(self.cbar_deep))
        self.link_power_scales.toggled.connect(lambda checked: self._link_power_from(self.cbar_fast) if checked else None)
        self.wf_window.valueChanged.connect(self._change_wf_window)
        self.wf_follow.toggled.connect(self._change_wf_follow)
        self.wf_follow.toggled.connect(self._update_return_live_button)
        for key, plot in [('wf_fast', self.wf_fast_plot), ('wf_phase', self.wf_phase_plot), ('wf_deep', self.wf_deep_plot)]:
            plot.vb.sigYRangeChanged.connect(lambda vb, ranges, key=key: self._on_wf_y_range(key, ranges))
            plot.vb.sigRangeChangedManually.connect(self._pause_wf_follow)
        self.marker.sigPositionChangeFinished.connect(self._commit_marker_bin)
        self.bin_minus_button.clicked.connect(self.step_bin_left)
        self.bin_plus_button.clicked.connect(self.step_bin_right)
        self.return_live_button.clicked.connect(self.return_to_live)
        self.pause_button.toggled.connect(self._update_return_live_button)
        self.follow_live.toggled.connect(self._update_return_live_button)

        # Phím tắt toàn cục
        self._shortcuts = [
            QtWidgets.QShortcut(QtGui.QKeySequence('L'), self, self.return_to_live),
            QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Space), self, self.toggle_pause_shortcut),
            QtWidgets.QShortcut(QtGui.QKeySequence('P'), self, self.snap_to_peak_s12),
            QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self, self.clear_drift_measurement),
            QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Left), self, self.step_bin_left),
            QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Right), self, self.step_bin_right),
        ]

        self.select_frequency(self.frequency.value())
        self.apply_mode()
        if self._persist_settings:
            self._restore_ui_settings()
        self._update_bin_buttons()
        self._update_return_live_button()

        from viewer_h5 import H5Source
        self.h5 = H5Source(self, toolbar_row, side)
        from rolling_controls import RollingController
        self.rolling = RollingController(self, side)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(25)

    def _sync_trace_time_ranges(self, source, ranges):
        if self._time_ranges_syncing:
            return
        self._time_ranges_syncing = True
        try:
            for plot in self.time_plots.values():
                if plot is not source:
                    plot.setXRange(*ranges, padding=0)
        finally:
            self._time_ranges_syncing = False

    def _on_fringe_mode_changed(self, *_):
        self.fringe_auto_y.setChecked(True)
        self._update_fringe_plot()

    def _on_fringe_range_manually(self, axes):
        if len(axes) > 1 and axes[1]:
            self.fringe_auto_y.setChecked(False)

    def _update_fringe_plot(self, *_):
        if self._is_h5():
            return self.h5.render_traces()
        if not hasattr(self, 'fringe_plot'):
            return
        self.fringe_re_curve.setVisible(self.fringe_re_check.isChecked())
        self.fringe_im_curve.setVisible(self.fringe_im_check.isChecked())
        normalized = self.fringe_mode.currentData() == 'normalized'
        self.fringe_re_check.setText('Real: γ cosφ' if normalized else 'Re(S12) / real')
        self.fringe_im_check.setText('Imaginary: γ sinφ' if normalized else 'Im(S12) / imaginary')
        self.fringe_plot.setLabel('left', 'Normalized R / I' if normalized else 'Cross power (ADC-count²/bin)')
        self.fringe_plot.setTitle(
            f'Bin {self.model.bin}, {self.model.actual:.6f} MHz — '
            + ('normalized correlation fringes (γ cosφ, γ sinφ)' if normalized else 'linear correlation fringes (Re / Im)'))
        if not self.fringe_plot.isVisible():
            return
        x, real, imag, self._fringe_values_by_key = build_fringe_series(
            self._display_rows, self._display_trace, self.model.bin,
            self.threshold.value(), normalized)
        self._fringe_render_error = False
        finite_arrays = [a[np.isfinite(a)] for a in (real, imag) if np.isfinite(a).any()]
        if not normalized and finite_arrays:
            low = min(0., min(float(a.min()) for a in finite_arrays))
            high = max(0., max(float(a.max()) for a in finite_arrays))
            span = high - low
            pad = max(span * .06, 1e-30)
            if not all(np.isfinite(value) for value in (span, low-pad, high+pad)):
                # Preserve reconstructed values in the readout; never feed an
                # infinite display range to Qt or retain a stale previous curve.
                self._fringe_render_error = True
                self.fringe_re_curve.setData([], [])
                self.fringe_im_curve.setData([], [])
                self.fringe_plot.setTitle('Fringes: linear range exceeds display precision — inspect readout')
                return
        self.fringe_re_curve.setData(x, real, connect='finite')
        self.fringe_im_curve.setData(x, imag, connect='finite')
        if not self.fringe_auto_y.isChecked():
            return
        if normalized:
            low, high = -1.05, 1.05
        else:
            arrays = [a[np.isfinite(a)] for curve, a in
                      ((self.fringe_re_curve, real), (self.fringe_im_curve, imag))
                      if curve.isVisible() and np.any(np.isfinite(a))]
            if not arrays:
                low, high = -1., 1.
            else:
                low = min(0., min(float(a.min()) for a in arrays))
                high = max(0., max(float(a.max()) for a in arrays))
                pad = max((high - low) * .06, 1e-30)
                low, high = low - pad, high + pad
        self.fringe_plot.setYRange(low, high, padding=0)

    def _update_bin_buttons(self):
        if self._is_h5():
            self.bin_minus_button.setEnabled(self.h5.worker is None and self.h5.catalog is not None)
            self.bin_plus_button.setEnabled(self.h5.worker is None and self.h5.catalog is not None)
            return
        enabled = not self.pause_button.isChecked()
        self.bin_minus_button.setEnabled(enabled and self.model.bin > 0)
        self.bin_plus_button.setEnabled(enabled and self.model.bin < BINS - 1)

    def _commit_marker_bin(self, *_):
        if self._is_h5():
            self.frequency.setValue(float(self.marker.value()))
            return
        if self.pause_button.isChecked():
            self.marker.setValue(self.model.actual)
            return
        value = float(self.marker.value())
        if not np.isfinite(value):
            self.marker.setValue(self.model.actual)
            return
        selected = nearest_frequency_bin(value)
        if selected == self.model.bin:
            # Selecting the same bin must not discard its time trace.
            self.marker.setValue(self.model.actual)
            return
        self.frequency.setValue(START + selected * STEP)
        self.marker.setValue(self.model.actual)

    def _update_return_live_button(self, *_):
        if self._is_h5():
            self.return_live_button.setEnabled(True)
            return
        self.return_live_button.setEnabled(self.pause_button.isChecked() or not self.follow_live.isChecked()
                                                 or not self.wf_follow.isChecked())

    def return_to_live(self, *_):
        if self._is_h5():
            self.h5.source.setCurrentIndex(0)
            if not self._is_h5():self.return_to_live()
            return
        self.pause_button.setChecked(False)
        self.clear_drift_measurement()
        self.reset_time_view()
        self.wf_follow.setChecked(True)
        self._update_wf_view()
        self.sync_displayed_curves()
        self.render()
        self._update_return_live_button()

    def _save_ui_settings(self):
        # Persist display preferences only: no network ports, data, or Pause state.
        state = {
            'version': 1,
            'mode': self.mode.currentText(),
            'frequency': self.frequency.value(),
            'threshold': self.threshold.value(),
            'time_window': self.time_window.value(),
            'fringe_mode': self.fringe_mode.currentData(),
            'fringe_re': self.fringe_re_check.isChecked(),
            'fringe_im': self.fringe_im_check.isChecked(),
            'fringe_auto_y': self.fringe_auto_y.isChecked(),
            'waterfall_window': self.wf_window.value(),
            'linked_power_scales': self.link_power_scales.isChecked(),
            'fast_power_levels': list(self.rolling.original_levels.get('live',(self.cbar_fast.values,self.cbar_deep.values))[0]) if hasattr(self,'rolling') and self.rolling.config.enabled else list(self.cbar_fast.values),
            'deep_power_levels': list(self.rolling.original_levels.get('live',(self.cbar_fast.values,self.cbar_deep.values))[1]) if hasattr(self,'rolling') and self.rolling.config.enabled else list(self.cbar_deep.values),
            'follow_live': self.follow_live.isChecked(),
            'autoscale_y': self.autoscale_y_check.isChecked(),
            'sidebar': self.sidebar_button.isChecked(),
            'focus': self.focus_combo.currentData(),
            'plots': {k: cb.isChecked() for k, cb in self.plot_checks.items()},
            'curves': {str(k): cb.isChecked() for k, cb in self.curve_checks.items()},
        }
        self.settings.setValue('ui/state_v1', json.dumps(state, allow_nan=False))
        self.settings.setValue('ui/geometry', self.saveGeometry())
        self.settings.setValue('ui/splitter', self.splitter.saveState())
        self.settings.sync()
        if self.settings.status() != QtCore.QSettings.NoError:
            print('Could not save viewer UI settings.', file=sys.stderr)

    def _restore_ui_settings(self):
        try:
            state = json.loads(str(self.settings.value('ui/state_v1', '{}')))
        except (TypeError, ValueError):
            return
        if not isinstance(state, dict) or state.get('version') != 1:
            return
        explicit = {arg.split('=', 1)[0] for arg in sys.argv[1:] if arg.startswith('--')}
        mode = state.get('mode')
        if '--mode' not in explicit and mode in ('CW', 'Solar'):
            self.mode.setCurrentText(mode)

        def number(key, widget, flag):
            value = state.get(key)
            if (flag not in explicit and type(value) in (int, float)
                    and widget.minimum() <= value <= widget.maximum() and np.isfinite(value)):
                if isinstance(widget, QtWidgets.QSpinBox):
                    if value != int(value):
                        return
                    value = int(value)
                widget.setValue(value)

        number('frequency', self.frequency, '--frequency-mhz')
        number('threshold', self.threshold, '--coherence-threshold')
        number('time_window', self.time_window, '--time-window')
        number('waterfall_window', self.wf_window, None)
        index = self.fringe_mode.findData(state.get('fringe_mode'))
        if index >= 0:
            self.fringe_mode.setCurrentIndex(index)
        for key, widget in [('fringe_re', self.fringe_re_check), ('fringe_im', self.fringe_im_check), ('fringe_auto_y', self.fringe_auto_y)]:
            if type(state.get(key)) is bool:
                widget.setChecked(state[key])
        if type(state.get('linked_power_scales')) is bool:
            self.link_power_scales.setChecked(state['linked_power_scales'])
        for key, bar in [('fast_power_levels', self.cbar_fast), ('deep_power_levels', self.cbar_deep)]:
            values = state.get(key)
            if (isinstance(values, list) and len(values) == 2
                    and all(type(x) in (int, float) and -1e6 <= x <= 1e6 for x in values)
                    and np.isfinite(values).all() and values[0] < values[1]):
                bar.setLevels(values)
        if self.link_power_scales.isChecked():
            self._link_power_from(self.cbar_fast)
        for key, widget in [('follow_live', self.follow_live), ('autoscale_y', self.autoscale_y_check),
                            ('sidebar', self.sidebar_button)]:
            if type(state.get(key)) is bool:
                widget.setChecked(state[key])
        # A CLI-selected workspace uses its own preset, not another mode's saved plots.
        if mode == self.mode.currentText():
            plots = state.get('plots', {})
            if isinstance(plots, dict):
                for key, cb in self.plot_checks.items():
                    if type(plots.get(key)) is bool:
                        cb.setChecked(plots[key])
            focus = state.get('focus')
            if focus is None or (isinstance(focus, str) and focus in self._plot_names):
                index = self.focus_combo.findData(focus)
                if index >= 0:
                    self.focus_combo.setCurrentIndex(index)
        curves = state.get('curves', {})
        if isinstance(curves, dict):
            for key, cb in self.curve_checks.items():
                if type(curves.get(str(key))) is bool:
                    cb.setChecked(curves[str(key)])
        geometry = self.settings.value('ui/geometry')
        splitter = self.settings.value('ui/splitter')
        if isinstance(geometry, QtCore.QByteArray):
            self.restoreGeometry(geometry)
        if isinstance(splitter, QtCore.QByteArray):
            self.splitter.restoreState(splitter)
        self.update_curve_visibility()
        self.update_layout_geometry()

    def _sync_frequency_ranges(self, source, ranges):
        # Explicit data ranges avoid GraphicsView pixel-overlap adjustments when
        # the spectrum master is removed from the scene in waterfall Focus mode.
        if self._frequency_syncing:
            return
        self._frequency_syncing = True
        try:
            for plot in (self.spectrum_plot, self.wf_fast_plot, self.wf_phase_plot, self.wf_deep_plot):
                if plot is not source:
                    plot.setXRange(*ranges, padding=0)
        finally:
            self._frequency_syncing = False

    def _wf_row_at(self, y):
        if not np.isfinite(y):
            return None
        index = int(np.floor(y)) - (self._wf_display_end - len(self._display_rows))
        return self._display_rows[index] if 0 <= index < len(self._display_rows) else None

    def _advance_wf_coordinates(self):
        latest = next((row for row in reversed(self._display_rows) if row is not None), None)
        if latest is None:
            return
        key = latest[0], latest[1]
        if self._wf_observation_key is None or key[0] != self._wf_observation_key[0]:
            self._wf_display_end = max(HISTORY, len(self._display_rows))
            self._wf_image_signature = None
            self.wf_follow.setChecked(True)
        else:
            self._wf_display_end += min(self.model.capacity, max(0, key[1] - self._wf_observation_key[1]))
        self._wf_observation_key = key

    def _apply_wf_range(self, low, high):
        self._wf_syncing = True
        try:
            for plot in (self.wf_fast_plot, self.wf_phase_plot, self.wf_deep_plot):
                plot.setYRange(float(low), float(high), padding=0)
            self._wf_view_range = tuple(self.wf_fast_plot.vb.viewRange()[1])
        finally:
            self._wf_syncing = False

    def _update_wf_view(self):
        span = int(np.ceil(self.wf_window.value() / PERIOD))
        end = self._wf_display_end
        floor = end - max(len(self._display_rows), span)
        self._wf_syncing = True
        try:
            for plot in (self.wf_fast_plot, self.wf_phase_plot, self.wf_deep_plot):
                plot.setLimits(yMin=floor, yMax=end, minYRange=2, maxYRange=end-floor)
        finally:
            self._wf_syncing = False
        if self.wf_follow.isChecked():
            low, high = end - span, end
        else:
            low, high = self._wf_view_range
            width = min(max(2, high-low), end-floor)
            low = min(max(low, floor), end-width)
            high = low + width
        self._apply_wf_range(low, high)
        rows = self._display_rows
        oldest = next((r for r in rows if r is not None), None)
        latest = next((r for r in reversed(rows) if r is not None), None)
        if oldest is not None and latest is not None:
            duration = max(0., (latest[2]-oldest[2])/1e6 + PERIOD)
            text = f'Retained: {duration:.1f} s · up to 30 min nominal'
            if not self.wf_follow.isChecked():
                text += '\nBROWSING HISTORY · L returns live'
        else:
            text = 'History: waiting · up to 30 min nominal in RAM'
        self.wf_history_label.setText(text)

    def _on_wf_y_range(self, key, ranges):
        if self._is_h5():
            return self.h5.sync_y(key, ranges)
        if self._wf_syncing:
            return
        self._apply_wf_range(*ranges)
        self._queue_wf_render()

    def _pause_wf_follow(self, *args):
        if self._is_h5():
            return
        if self._wf_syncing:
            return
        if args and isinstance(args[0], (tuple, list)) and len(args[0]) > 1 and not args[0][1]:
            return
        self.wf_follow.setChecked(False)

    def _change_wf_follow(self, *_):
        if self._is_h5():
            return
        self._update_wf_view()
        self._queue_wf_render()

    def _change_wf_window(self, *_):
        if self._is_h5():
            return
        if self.wf_follow.isChecked():
            self._update_wf_view()
        else:
            # Change the span around the currently inspected time, not around live.
            low, high = self._wf_view_range
            span = int(np.ceil(self.wf_window.value() / PERIOD))
            self._apply_wf_range(high-span, high)
            self._update_wf_view()
        self._queue_wf_render()

    def _queue_wf_render(self, *_):
        if self._is_h5():
            return self.h5.render_waterfalls()
        if not hasattr(self, 'wf_fast_plot') or self._wf_render_pending:
            return
        self._wf_render_pending = True
        QtCore.QTimer.singleShot(0, self._finish_wf_render)

    def _finish_wf_render(self):
        self._wf_render_pending = False
        if hasattr(self, 'img_phase'):
            self._render_wf_window()

    def _push_deep_observation(self, session, fid, stamp, count, values):
        new_session = session != self._deep_source_session
        if new_session:
            self.deep_history.clear()
            self.buf_deep.reset()
            self.buf_deep.last_session = session
            self.latest_deep_db = np.full(BINS, np.nan, dtype=np.float32)
            self._deep_source_session = session
        previous = self.integrator.last_frame_id
        if not new_session and previous is not None and fid <= previous:
            return
        if new_session or self.integrator.count == 0 or self.integrator.time_break(stamp, count) or (previous is not None and fid != previous+1):
            self._deep_group_start = fid, stamp
        start_fid, start_stamp = self._deep_group_start
        for deep in self.integrator.push(session, fid, stamp, count, values):
            self.buf_deep.push_frame(deep[0], deep[1], deep[4])
            self.latest_deep_db = np.asarray(deep[4], dtype=np.float32)
            # Synthetic NaN gap outputs are not completed integrations.
            if np.isfinite(deep[4]).any():
                self.deep_history.append((session, deep[1], start_fid, fid, start_stamp, stamp, deep[4]))

    def _deep_group_at(self, y):
        row = self._wf_row_at(y)
        if row is None:
            return None
        for group in reversed(self._display_deep_rows):
            if group[0] == row[0] and group[2] <= row[1] <= group[3]:
                return group
        return None

    def _link_power_from(self, source):
        if self._power_levels_syncing or not self.link_power_scales.isChecked():
            return
        self._power_levels_syncing = True
        try:
            other = self.cbar_deep if source is self.cbar_fast else self.cbar_fast
            levels = tuple(source.values)
            if tuple(other.values) != levels:
                other.setLevels(levels)
            self._last_linked_power_levels = levels
        finally:
            self._power_levels_syncing = False

    def _render_wf_window(self):
        if self._is_h5():
            return self.h5.render_waterfalls()
        if self.link_power_scales.isChecked():
            source = self.cbar_fast
            if (tuple(self.cbar_fast.values) == getattr(self, '_last_linked_power_levels', None)
                    and tuple(self.cbar_deep.values) != tuple(self.cbar_fast.values)):
                source = self.cbar_deep
            self._link_power_from(source)
        show_fast = self.wf_fast_plot.isVisible()
        show_phase = self.wf_phase_plot.isVisible()
        show_deep = self.wf_deep_plot.isVisible()
        if not show_fast and not show_phase and not show_deep:
            return
        low, high = self._wf_view_range
        base = self._wf_display_end - len(self._display_rows)
        first = max(base, int(np.floor(low))-1)
        last = min(self._wf_display_end, int(np.ceil(high))+1)
        subset = self._display_rows[first-base:last-base] if last > first else ()
        valid = [row for row in subset if row is not None]
        groups = tuple(g for g in self._display_deep_rows
                       if valid and g[0] == valid[0][0] and g[3] >= valid[0][1] and g[2] <= valid[-1][1])
        threshold = self.threshold.value()
        signature = (first, last, tuple(id(r) for r in subset), tuple(id(g) for g in groups),
                     threshold, show_fast, show_phase, show_deep)
        if signature == self._wf_image_signature:
            return
        self._wf_image_signature = signature
        height = max(1, last-first)
        rect = QtCore.QRectF(START-STEP/2, first, STOP-START, height)
        if show_fast:
            data = np.full((height, BINS), np.nan, dtype=np.float32)
            for i, row in enumerate(subset):
                if row is not None:
                    data[i] = self.rolling.fast_values(row) if hasattr(self,"rolling") else row[4].get(S12,np.nan)
            self.img_fast.setImage(data, autoLevels=False)
            self.img_fast.setRect(rect)
        if show_phase:
            data = np.full((height, BINS), np.nan, dtype=np.float32)
            for i, row in enumerate(subset):
                if row is not None:
                    data[i] = masked_phase(row[4], threshold)
            self.img_phase.setImage(data, autoLevels=False)
            self.img_phase.setRect(rect)
        if show_deep:
            data = np.full((height, BINS), np.nan, dtype=np.float32)
            ends = np.asarray([g[3] for g in groups], dtype=np.uint64)
            for i, row in enumerate(subset):
                if row is None:
                    continue
                index = int(np.searchsorted(ends, np.uint64(row[1])))
                if index < len(groups) and groups[index][2] <= row[1] <= groups[index][3]:
                    # Hold the group's single mean across its actual source-frame
                    # rows. This is a block display, not additional integration.
                    data[i] = self.rolling.deep_values(groups[index]) if hasattr(self,"rolling") else groups[index][6]
            self.img_deep.setImage(data, autoLevels=False)
            self.img_deep.setRect(rect)

    def _theme_window(self):
        ui.apply_window_theme(self)

    def _theme_plots(self):
        ui.style_plots(self.all_plots)
        ui.style_colorbars((self.cbar_fast, self.cbar_phase, self.cbar_deep))
        self.phase_plot.setLabel('bottom', 'Vietnam time (UTC+07:00)')
        self.gamma_plot.setLabel('bottom', 'Vietnam time (UTC+07:00)')
        self.phase_points.setSize(4)
        now = time.time()
        self.phase_plot.setXRange(now - self.time_window.value(), now, padding=0)

    def _plot_is_visible(self, key):
        return key == self._focus_key if self._focus_key is not None else self.plot_checks[key].isChecked()

    def _change_focus(self, *_):
        self._focus_key = self.focus_combo.currentData()
        for cb in self.plot_checks.values():
            cb.setEnabled(self._focus_key is None)
        self.update_layout_geometry()

    def _tick(self):
        # Keep the validated receive/join/integrate path in update_data unchanged.
        if self._is_h5():
            self.update_data()
            self.h5.status()
            return
        self.update_data()
        key = (self.model.session, self.model.last_id)
        if self.model.last_id is not None and key != self._last_observation_key:
            self._last_observation_key = key
            self._last_observation_received = time.monotonic()
        age = (time.monotonic() - self._last_observation_received
               if self._last_observation_received is not None else None)
        if age is None:
            text, color = 'WAITING FOR FRAMES', '#91a4b9'
        elif age > 3:
            text, color = 'NO NEW MATCHED FRAME > 3 s', '#e2bb71'
        else:
            text, color = 'RECEIVING MATCHED FRAMES', '#79d7bf'
        age_text = 'Last matched: —' if age is None else f'Last matched: {age:.1f} s ago'
        if self._paused_since is not None:
            age_text += f' · display held {time.monotonic() - self._paused_since:.1f} s'
        if self.data_age_label.text() != age_text:
            self.data_age_label.setText(age_text)
            self.data_age_label.setToolTip(age_text + '\nLocal time since the newest matched observation was processed; not capture age or network latency.')
        if text != self.connection_badge.text():
            self.connection_badge.setText(text)
            self.connection_badge.setStyleSheet(f'color: {color};')

    def _update_dashboard_metrics(self, threshold):
        self.metric_values['frequency'].setText(f'{self.model.actual:.6f}')
        self.metric_details['frequency'].setText(f'MHz · bin {self.model.bin}')
        sample = next((r for r in reversed(self._display_trace) if r is not None), None)
        if sample is None:
            for key in ('phase', 'gamma', 'time'):
                self.metric_values[key].setText('—')
                self.metric_details[key].setText('Waiting for selected-bin data')
        else:
            session, fid, stamp, count, phase, gamma = sample
            quality = self._classify_phase_quality(phase, gamma, threshold)
            self.metric_values['phase'].setText(f'{np.rad2deg(phase):+.2f}°' if np.isfinite(phase) else '—')
            self.metric_details['phase'].setText('VALID · unsmoothed' if quality == 'VALID' else quality)
            self.metric_values['phase'].setStyleSheet('color: #b9a8ef;' if quality == 'VALID' else 'color: #c6a16a;')
            self.metric_values['gamma'].setText(f'{gamma:.4f}' if np.isfinite(gamma) else '—')
            self.metric_details['gamma'].setText(f'Display threshold ≥ {threshold:.2f}')
            self.metric_values['gamma'].setStyleSheet('color: #7ad4bb;')
            stamp_text = format_vn_time(stamp / 1e6, '%H:%M:%S.%f', milliseconds=True)
            self.metric_values['time'].setText(stamp_text)
            self.metric_details['time'].setText(f'Frame {fid} · UTC+07:00')
        for key, label in self.metric_values.items():
            label.setToolTip(label.text() + '\n' + self.metric_details[key].text())
            self.metric_details[key].setToolTip(self.metric_details[key].text())
        self.readout_pinned.setToolTip(self.readout_pinned.text())

    def _show_shortcuts(self):
        QtWidgets.QMessageBox.information(self, 'Observation controls',
            'Space   Pause / resume display (reception continues)\n'
            'L       Return to live (resume + follow latest time)\n'
            'P       Select peak |S12| bin\n'
            '← / →   Step one frequency bin\n'
            'Esc     Clear drift measurement\n\n'
            'Click spectrum / double-click waterfall: select a bin.\n'
            'Drag and release the purple marker: snap to nearest bin.\n'
            'Shift+Click two waterfall points: measure drift; first click pauses.\n'
            'Resume clears the measurement. Deep measurements use group end timestamps.\n\n'
            'Focus enlarges one plot; Overview restores your plot choices.\n'
            'Waterfall: drag/zoom time to browse retained history (30 min).\n'
            'Return to live restores both waterfall and trace following.\n'
            'Raw phase remains visible in readouts with its quality status;\n'
            'phase/fringe plots apply the coherence display mask.\n'
            'Fringes: normalized γ cosφ / γ sinφ or linear Re/Im from 10^(S12_dB/10).\n'
            'Linear samples at/below the backend -120 dB floor are hidden.\n'
            'Hover a readout\n'
            'for full text. Bin and threshold controls are locked during Pause.')

    def _init_crosshairs_and_overlays(self):
        dash_pen = pg.mkPen('#94a3b8', width=1, style=QtCore.Qt.DashLine)
        self.freq_plots = {
            'spectrum': self.spectrum_plot,
            'wf_fast': self.wf_fast_plot,
            'wf_phase': self.wf_phase_plot,
            'wf_deep': self.wf_deep_plot,
        }
        self.time_plots = {
            'power': self.power_plot,
            'phase': self.phase_plot,
            'fringe': self.fringe_plot,
            'gamma': self.gamma_plot,
        }
        self.crosshair_v = {}
        self.crosshair_h = {}
        self.hover_tooltips = {}

        for key, plot in {**self.freq_plots, **self.time_plots}.items():
            vline = pg.InfiniteLine(angle=90, movable=False, pen=dash_pen)
            hline = pg.InfiniteLine(angle=0, movable=False, pen=dash_pen)
            vline.setVisible(False)
            hline.setVisible(False)
            vline.setZValue(50)
            hline.setZValue(50)
            plot.addItem(vline, ignoreBounds=True)
            plot.addItem(hline, ignoreBounds=True)
            self.crosshair_v[key] = vline
            self.crosshair_h[key] = hline

            tip = pg.TextItem(
                text='',
                color='#f8fafc',
                fill=pg.mkBrush(15, 23, 42, 220),
                border=pg.mkPen('#475569', width=1),
                anchor=(0, 0),
            )
            tip.setZValue(60)
            tip.setVisible(False)
            plot.addItem(tip, ignoreBounds=True)
            self.hover_tooltips[key] = tip

        # Lớp vẽ thước đo độ dốc trôi tần (Drift Rate Overlay) trên 3 đồ thị Waterfall
        self.drift_items = {}
        drift_pen = pg.mkPen('#38bdf8', width=2, style=QtCore.Qt.DashLine)
        for key in ('wf_fast', 'wf_phase', 'wf_deep'):
            plot = self.freq_plots[key]
            line_item = pg.PlotDataItem(
                pen=drift_pen,
                symbol='o',
                symbolSize=8,
                symbolBrush=pg.mkBrush('#facc15'),
                symbolPen=pg.mkPen('#0f172a', width=1.5),
            )
            line_item.setZValue(55)
            line_item.setVisible(False)
            plot.addItem(line_item, ignoreBounds=True)
            self.drift_items[key] = line_item

    def make_waterfall(self, row, title, phase):
        plot = self.graphics.addPlot(
            row=row,
            col=0,
            title=title,
            axisItems={'left': SlotTimeAxis(self.model, orientation='left')},
        )
        plot.setLabel('bottom', 'Frequency', units='MHz')
        plot.getAxis('bottom').enableAutoSIPrefix(False)
        plot.setLabel('left', 'VN time')
        plot.getAxis('left').setWidth(92)
        plot.setXRange(START-STEP/2, STOP-STEP/2, padding=0)
        plot.setYRange(0, HISTORY, padding=0)
        plot.setMouseEnabled(x=True, y=True)
        plot.enableAutoRange(x=False, y=False)
        image = pg.ImageItem(np.full((HISTORY, BINS), np.nan, dtype=np.float32), axisOrder='row-major', levels=(0, 1))
        plot.addItem(image)
        image.setRect(QtCore.QRectF(START - STEP / 2, 0, STOP - START, HISTORY))
        if phase:
            colors = [tuple(round(c * 255) for c in colorsys.hsv_to_rgb(i / 256, 1, 1)) for i in range(257)]
            colors[-1] = colors[0]
            cmap = pg.ColorMap(np.linspace(0, 1, 257), colors)
        else:
            cmap = pg.colormap.get('inferno')
        bar = pg.ColorBarItem(
            values=(-180, 180) if phase else (-60, 85),
            colorMap=cmap,
            label='Phase °' if phase else '|S12| dB',
            interactive=not phase,
        )
        bar.setImageItem(image)
        self.graphics.addItem(bar, row=row, col=1)
        return plot, image, bar

    # --- Quản lý Bật/Tắt Đồ thị, Đường cong & Tự động Scale Bố cục ---

    def apply_mode(self, *_):
        self.focus_combo.setCurrentIndex(0)
        self._focus_key = None
        self.workspace_title.setText(self.mode.currentText().upper() + (' / H5 OBSERVATION' if self._is_h5() else ' OBSERVATION'))
        solar = self.mode.currentText() == 'Solar'
        preset = {
            'power': True,
            'spectrum': True,
            'phase': True,
            'fringe': not solar,
            'gamma': True,
            'wf_fast': True,
            'wf_phase': solar,
            'wf_deep': solar,
        }
        for key, visible in preset.items():
            cb = self.plot_checks[key]
            cb.blockSignals(True)
            cb.setChecked(visible)
            cb.blockSignals(False)
        self.update_layout_geometry()

    def update_layout_geometry(self, *_):
        if not hasattr(self, "crosshair_v"):
            return
        row_configs = [
            (0, 'spectrum', self.spectrum_plot, None, 2),
            (7, 'power', self.power_plot, None, 2),
            (1, 'phase', self.phase_plot, None, 2),
            (2, 'fringe', self.fringe_plot, None, 3),
            (3, 'gamma', self.gamma_plot, None, 2),
            (4, 'wf_fast', self.wf_fast_plot, self.cbar_fast, 3),
            (5, 'wf_phase', self.wf_phase_plot, self.cbar_phase, 3),
            (6, 'wf_deep', self.wf_deep_plot, self.cbar_deep, 3),
        ]
        wf_visible_count = sum(
            1 for key in ('wf_fast', 'wf_phase', 'wf_deep') if self._plot_is_visible(key)
        )
        grid = self.graphics.ci.layout

        for row_idx, key, plot, cbar, default_stretch in row_configs:
            visible = self._plot_is_visible(key)
            # Hidden QGraphicsWidgets can retain minimum/preferred size hints.
            # Remove them through GraphicsLayout so its bookkeeping stays valid.
            for item, col, span in ((plot, 0, 2 if cbar is None else 1), (cbar, 1, 1)):
                if item is None:
                    continue
                present = item in self.graphics.ci.items
                if visible and not present:
                    self.graphics.ci.addItem(item, row=row_idx, col=col, colspan=span)
                elif not visible and present:
                    self.graphics.ci.removeItem(item)
                item.setVisible(visible)
            if visible:
                plot.getAxis('left').setWidth(125 if self._is_h5() and key.startswith('wf_') else 92)
                stretch = (5 if key.startswith('wf_') else 2) if self.mode.currentText() == 'Solar' else default_stretch
                if key == 'phase' and wf_visible_count <= 1:
                    stretch = 4
                grid.setRowMinimumHeight(row_idx, 170 if key.startswith('wf_') else 140)
                grid.setRowMaximumHeight(row_idx, 16777215)
                grid.setRowPreferredHeight(row_idx, -1)
                grid.setRowStretchFactor(row_idx, stretch)
                grid.setRowSpacing(row_idx, 4)
            else:
                grid.setRowFixedHeight(row_idx, 0)
                grid.setRowSpacing(row_idx, 0)
                grid.setRowStretchFactor(row_idx, 0)
                if key in self.crosshair_v:
                    self.crosshair_v[key].setVisible(False)
                    self.crosshair_h[key].setVisible(False)
                    self.hover_tooltips[key].setVisible(False)

        # Cấu hình co giãn 2 cột: Cột 0 (đồ thị) co giãn 100% để luôn chiếm trọn chiều ngang
        grid.setColumnStretchFactor(0, 1)
        grid.setColumnStretchFactor(1, 0)
        if wf_visible_count > 0:
            grid.setColumnFixedWidth(1, 84)
            grid.setColumnSpacing(0, 4)
        else:
            grid.setColumnFixedWidth(1, 0)
            grid.setColumnSpacing(0, 0)

        visible_rows = [r for r, key, *_ in row_configs if self._plot_is_visible(key)]
        for row_idx, key, *_ in row_configs:
            grid.setRowSpacing(row_idx, 4 if visible_rows and row_idx in visible_rows[:-1] else 0)
        grid.invalidate()
        minimum = grid.effectiveSizeHint(QtCore.Qt.MinimumSize)
        # Keep axes readable: below this height Qt cannot fit all selected plots.
        self.graphics.setMinimumHeight(max(1, int(np.ceil(minimum.height()))))
        self.graphics.setMinimumWidth(max(1, int(np.ceil(minimum.width()))))
        grid.activate()

        self.empty_label.setVisible(not bool(visible_rows))
        self._update_fringe_plot()
        self._queue_wf_render()
        self._sync_viewport_geometry()
        self._schedule_layout_sync()

    def _sync_viewport_geometry(self):
        if not hasattr(self, 'graphics'):
            return
        size = self.graphics.viewport().size()
        if size.width() <= 0 or size.height() <= 0:
            return
        ci = self.graphics.ci
        # Explicit size hints also cover an empty grid (all plots disabled).
        ci.setMinimumSize(size.width(), size.height())
        ci.setMaximumSize(size.width(), size.height())
        ci.setGeometry(QtCore.QRectF(0, 0, size.width(), size.height()))
        ci.layout.activate()

    def _schedule_layout_sync(self, *_):
        if not self._layout_pending:
            self._layout_pending = True
            QtCore.QTimer.singleShot(0, self._finish_layout_sync)

    def _finish_layout_sync(self):
        self._layout_pending = False
        self._sync_viewport_geometry()

    def eventFilter(self, watched, event):
        if (hasattr(self, 'graphics') and watched is self.graphics.viewport()
                and event.type() in (QtCore.QEvent.Resize, QtCore.QEvent.Show)):
            self._schedule_layout_sync()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'graphics'):
            self._schedule_layout_sync()

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_layout_sync()

    def update_curve_visibility(self, *_):
        if self._is_h5():
            return self.h5.render()
        for kind, curve in self.curves.items():
            curve.setVisible(self.curve_checks[kind].isChecked())
        self.deep_curve.setVisible(self.curve_checks['DEEP'].isChecked())
        self.update_peak_marker_and_metrics()
        if self.autoscale_y_check.isChecked():
            self.fit_spectrum_y()

    def on_autoscale_y_toggled(self, checked):
        if checked:
            self.fit_spectrum_y()

    def on_spectrum_range_changed_manually(self, *args):
        if self._updating_spectrum_y:
            return
        if args and isinstance(args[0], (list, tuple)) and len(args[0]) >= 2:
            if not args[0][1]:
                return
        self.autoscale_y_check.blockSignals(True)
        self.autoscale_y_check.setChecked(False)
        self.autoscale_y_check.blockSignals(False)

    def fit_spectrum_y(self, *_):
        if self._is_h5():
            return self.h5.fit_spectrum()
        visible_arrays = []
        for kind in (S12, S11, S22):
            if self.curves[kind].isVisible():
                arr = self.displayed_streams.get(kind)
                if arr is not None:
                    finite = arr[np.isfinite(arr)]
                    if finite.size > 0:
                        visible_arrays.append(finite)
        if self.deep_curve.isVisible() and self.displayed_deep_db is not None:
            finite_deep = self.displayed_deep_db[np.isfinite(self.displayed_deep_db)]
            if finite_deep.size > 0:
                visible_arrays.append(finite_deep)
        if not visible_arrays:
            return
        ymin = float(min(np.min(a) for a in visible_arrays))
        ymax = float(max(np.max(a) for a in visible_arrays))
        if not np.isfinite(ymin) or not np.isfinite(ymax):
            return
        if ymax <= ymin:
            ymax = ymin + 1.0
        self._updating_spectrum_y = True
        try:
            self.spectrum_plot.setYRange(ymin - 3.0, ymax + 3.0, padding=0)
        finally:
            self._updating_spectrum_y = False

    # --- Quản lý Trục Thời gian & Chọn Tần số / Bin ---

    def pause_follow(self, *_):
        if not self._updating_time_x:
            self.follow_live.setChecked(False)

    def update_time_view(self, *_):
        if self._is_h5():
            return
        if self.pause_button.isChecked() or not self.follow_live.isChecked() or not self.model.trace:
            return
        latest = next((row for row in reversed(self.model.trace) if row is not None), None)
        if latest is not None:
            right = latest[2] / 1e6 + PERIOD * .5
            self._updating_time_x = True
            try:
                self.phase_plot.setXRange(right - self.time_window.value(), right, padding=0)
            finally:
                self._updating_time_x = False

    def reset_time_view(self, *_):
        if self._is_h5():
            return self.h5.fit_time()
        self.phase_plot.setYRange(-180, 180, padding=0)
        self.gamma_plot.setYRange(0, 1.05, padding=0)
        self.follow_live.setChecked(True)
        self.update_time_view()

    def _update_power_plot(self):
        if not hasattr(self,'power_curves'):return
        if self._is_h5():
            if self.h5.data is not None:self.h5.render_power()
            return
        if self.pause_button.isChecked():
            for key,curve in self.power_curves.items():curve.setVisible(self.power_checks[key].isChecked())
            return
        x,a,b,c=self.model.power_time_series()
        for key,values in ((S11,a),(S22,b),(S12,c)):
            self.power_curves[key].setData(x,values,connect='finite')
            self.power_curves[key].setVisible(self.power_checks[key].isChecked())
        self.power_plot.setTitle(f'Power vs time — bin {self.model.bin} / {self.model.actual:.6f} MHz (no coherence mask)')

    def select_frequency(self, frequency):
        if self._is_h5():
            return self.h5.select(frequency)
        if self.pause_button.isChecked():
            return
        if nearest_frequency_bin(frequency) != self.model.bin:
            self.model.select(frequency)
        else:
            self.model.requested = float(frequency)
        self.marker.setValue(self.model.actual)
        self._update_bin_buttons()
        self.bin_label.setText(f'Bin {self.model.bin} → {self.model.actual:.6f} MHz')
        self.render()

    def step_bin(self, delta_bins):
        if self._is_h5():
            return self.h5.step(delta_bins)
        if self.pause_button.isChecked():
            return
        target_bin = int(np.clip(self.model.bin + int(delta_bins), 0, BINS - 1))
        target_freq = START + target_bin * STEP
        self.frequency.setValue(float(np.clip(target_freq, START, STOP - STEP)))

    def step_bin_left(self):
        self.step_bin(-1)

    def step_bin_right(self):
        self.step_bin(1)

    def toggle_pause_shortcut(self):
        if self._is_h5():
            return
        self.pause_button.setChecked(not self.pause_button.isChecked())

    def on_pause_toggled(self, paused):
        if self._is_h5():
            return
        self._paused_since = time.monotonic() if paused else None
        self.marker.setMovable(not paused)
        self._update_bin_buttons()
        self.frequency.setEnabled(not paused)
        self.threshold.setEnabled(not paused)
        self.snap_peak_button.setEnabled(not paused)
        self._hide_all_hover_overlays()
        if paused:
            self._display_rows = tuple(self._display_rows)  # Receiver observations are immutable after emission.
            self._display_trace = tuple(self._display_trace)
            for plot in (self.wf_fast_plot, self.wf_phase_plot, self.wf_deep_plot):
                plot.getAxis('left').display_rows = self._display_rows
            self.readout_pinned.setText(self.readout_pinned.text().replace('PINNED:', 'PINNED [PAUSED]:', 1))
            self.readout_pinned.setToolTip(self.readout_pinned.text())
            self.pause_button.setText('Resume')
            self.display_badge.setText('DISPLAY PAUSED')
            self.pause_button.setStyleSheet('background: #80632b; color: #fff0ce; border: 1px solid #b38a3a;')
        else:
            self.pause_button.setText('Pause')
            self.display_badge.setText('DISPLAY LIVE')
            self.pause_button.setStyleSheet('')
            self.clear_drift_measurement()
            self.sync_displayed_curves()
            self.render()

    def keyPressEvent(self, event):
        key = event.key()
        if key == QtCore.Qt.Key_Left:
            self.step_bin_left()
            event.accept()
            return
        if key == QtCore.Qt.Key_Right:
            self.step_bin_right()
            event.accept()
            return
        if key == QtCore.Qt.Key_Space:
            self.toggle_pause_shortcut()
            event.accept()
            return
        if key == QtCore.Qt.Key_P:
            self.snap_to_peak_s12()
            event.accept()
            return
        if key == QtCore.Qt.Key_Escape:
            self.clear_drift_measurement()
            event.accept()
            return
        super().keyPressEvent(event)

    # --- Đỉnh Phổ |S12| & Nút Snap to Peak ---

    def update_peak_marker_and_metrics(self):
        if self._is_h5():
            self.peak_marker.hide()
            return
        s12 = self.displayed_streams.get(S12)
        if s12 is None or not np.isfinite(s12).any():
            self.peak_marker.setData([], [])
            self.peak_summary_text = 'Peak |S12|: waiting for spectrum data'
            self.update_metrics_readout()
            return
        peak_bin = int(np.argmax(np.where(np.isfinite(s12), s12, -np.inf)))
        peak_val = float(s12[peak_bin])
        peak_freq = START + peak_bin * STEP
        floor_val = float(np.median(s12[np.isfinite(s12)]))
        snr_db = peak_val - floor_val
        if self.curve_checks[S12].isChecked():
            self.peak_marker.setData([peak_freq], [peak_val])
        else:
            self.peak_marker.setData([], [])
        self.peak_summary_text = (
            f'Peak |S12|: {peak_val:+.2f} dB @ {peak_freq:.6f} MHz '
            f'(Bin {peak_bin}, Median {floor_val:+.2f} dB, Peak−median {snr_db:+.2f} dB; not calibrated SNR)'
        )
        self.update_metrics_readout()

    def snap_to_peak_s12(self, *_):
        if self._is_h5():
            return self.h5.snap()
        if self.pause_button.isChecked():
            return
        s12 = self.displayed_streams.get(S12)
        if s12 is None or not np.isfinite(s12).any():
            s12 = self.latest_streams.get(S12)
        if s12 is None or not np.isfinite(s12).any():
            return
        peak_bin = int(np.argmax(np.where(np.isfinite(s12), s12, -np.inf)))
        peak_freq = START + peak_bin * STEP
        self.frequency.setValue(float(np.clip(peak_freq, START, STOP - STEP)))

    # --- Thước đo Độ dốc Trôi tần 2 điểm (Drift Rate Tool) ---

    def _waterfall_row_timestamp_seconds(self, plot_key, y_val):
        if plot_key == 'wf_deep':
            group = self._deep_group_at(y_val)
            return ((group[5] / 1e6, int(np.floor(y_val)), True) if group is not None
                    else (np.nan, int(np.floor(y_val)), False))
        row_idx = int(np.floor(y_val))
        row = self._wf_row_at(y_val)
        if row is not None:
            return float(row[2]) / 1e6, row_idx, True
        return np.nan, row_idx, False

    def _waterfall_point_is_valid(self, plot_key, y, bin_index):
        if plot_key == 'wf_deep':
            group = self._deep_group_at(y)
            return group is not None and np.isfinite(self.rolling.deep_values(group)[bin_index])
        row = self._wf_row_at(y)
        if row is None:
            return False
        streams = row[4]
        if plot_key == 'wf_fast':
            values = self.rolling.fast_values(row)
            return values is not None and np.isfinite(values[bin_index])
        if plot_key == 'wf_phase':
            phase, gamma = streams.get(PHASE), streams.get(COHERENCE)
            return (phase is not None and gamma is not None and
                    self._classify_phase_quality(float(phase[bin_index]), float(gamma[bin_index]),
                                                 self.threshold.value()) == 'VALID')
        return False

    def handle_drift_click(self, plot_key, freq_x, y_val):
        if self._is_h5():
            return self.h5.measure(plot_key, freq_x, y_val)
        low, high = self._wf_display_end - len(self._display_rows), self._wf_display_end
        if not (np.isfinite(freq_x) and np.isfinite(y_val) and low <= y_val < high):
            return
        # Both endpoints must refer to the same displayed waterfall generation.
        self.pause_button.setChecked(True)
        b = nearest_frequency_bin(freq_x)
        f_bin = START + b * STEP
        t_sec, slot_idx, exact_utc = self._waterfall_row_timestamp_seconds(plot_key, y_val)
        if not np.isfinite(t_sec) or not self._waterfall_point_is_valid(plot_key, y_val, b):
            self.drift_summary_text = 'Drift Measure: no valid displayed data/timestamp here (gap or mask); choose a valid point'
            self.update_metrics_readout()
            return

        if self.drift_plot_key != plot_key or len(self.drift_points) >= 2:
            for item in self.drift_items.values():
                item.setVisible(False)
                item.setData([], [])
            self.drift_plot_key = plot_key
            self.drift_points = [(f_bin, float(y_val), t_sec, b, slot_idx, exact_utc)]
            item = self.drift_items[plot_key]
            item.setData([f_bin], [float(y_val)])
            item.setVisible(True)
            self.drift_summary_text = (
                f'Drift Measure [{plot_key}]: Pt A = {f_bin:.6f} MHz (Bin {b}) '
                f'→ Shift+Click Pt B on same Waterfall'
            )
            self.update_metrics_readout()
            return

        self.drift_points.append((f_bin, float(y_val), t_sec, b, slot_idx, exact_utc))
        pt_a, pt_b = self.drift_points[0], self.drift_points[1]
        item = self.drift_items[plot_key]
        item.setData([pt_a[0], pt_b[0]], [pt_a[1], pt_b[1]])
        item.setVisible(True)

        df = pt_b[0] - pt_a[0]
        if pt_a[5] and pt_b[5]:
            dt = pt_b[2] - pt_a[2]
            dt_mode = 'UTC group-end' if plot_key == 'wf_deep' else 'UTC'
        elif plot_key == 'wf_deep':
            dt = (pt_b[4] - pt_a[4]) * self.deep_frames * PERIOD
            dt_mode = 'deep-slot ESTIMATE; assumes contiguous nominal cadence'
        else:
            dt = (pt_b[4] - pt_a[4]) * PERIOD
            dt_mode = 'slot-est'

        if abs(dt) > 1e-9:
            rate_text = f'{df / dt:+.6f} MHz/s'
        else:
            rate_text = 'undefined (Δt=0)'

        self.drift_summary_text = (
            f'Drift Measure: f1={pt_a[0]:.4f} -> f2={pt_b[0]:.4f} MHz '
            f'(Δf={df:+.4f} MHz) | Δt={dt:+.3f} s ({dt_mode}) | df/dt = {rate_text}'
        )
        self.update_metrics_readout()

    def clear_drift_measurement(self, *_):
        self.drift_plot_key = None
        self.drift_points.clear()
        for item in self.drift_items.values():
            item.setVisible(False)
            item.setData([], [])
        self.drift_summary_text = 'Drift Measure: Shift+Click 2 points on any Waterfall to measure Δf/Δt'
        self.update_metrics_readout()

    def update_metrics_readout(self):
        self.readout_metrics.setText(f'{self.peak_summary_text} || {self.drift_summary_text}')
        self.readout_metrics.setToolTip(self.readout_metrics.text())

    # --- Sự kiện Click & Rê chuột (Crosshair + Multi-Plot Hover Readout) ---

    def scene_mouse_clicked(self, event):
        if self._is_h5():
            return self.h5.mouse_click(event)
        if event.button() != QtCore.Qt.LeftButton:
            return
        pos = event.scenePos()
        modifiers = event.modifiers()

        # Kiểm tra click trên Spectrum 1D
        if self.spectrum_plot.isVisible() and self.spectrum_plot.vb.sceneBoundingRect().contains(pos):
            view_pos = self.spectrum_plot.vb.mapSceneToView(pos)
            if not self.pause_button.isChecked():
                self.frequency.setValue(START + nearest_frequency_bin(float(view_pos.x())) * STEP)
            event.accept()
            return

        # Kiểm tra click / double-click / Shift+Click trên 3 đồ thị Waterfall
        for key in ('wf_fast', 'wf_phase', 'wf_deep'):
            plot = self.freq_plots[key]
            if plot.isVisible() and plot.vb.sceneBoundingRect().contains(pos):
                view_pos = plot.vb.mapSceneToView(pos)
                freq_x = float(np.clip(view_pos.x(), START, STOP - STEP))
                y_val = float(view_pos.y())
                if modifiers & QtCore.Qt.ShiftModifier:
                    self.handle_drift_click(key, freq_x, y_val)
                    event.accept()
                    return
                if event.double() and not self.pause_button.isChecked():
                    self.frequency.setValue(START + nearest_frequency_bin(freq_x) * STEP)
                    event.accept()
                    return

    def spectrum_clicked(self, event):
        """Giữ phương thức tương thích ngược nếu được gọi trực tiếp."""
        self.scene_mouse_clicked(event)

    def _hide_all_hover_overlays(self):
        for vline in self.crosshair_v.values():
            vline.setVisible(False)
        for hline in self.crosshair_h.values():
            hline.setVisible(False)
        for tip in self.hover_tooltips.values():
            tip.setVisible(False)

    def _place_tooltip(self, key, plot, x_val, y_val, text):
        for k, tip in self.hover_tooltips.items():
            if k != key:
                tip.setVisible(False)
        tip = self.hover_tooltips[key]
        tip.setText(text)
        tip.setAnchor((0, 0))
        tip.setPos(x_val, y_val)
        tip.setVisible(True)
        # Clamp using measured scene pixels, not data-axis direction or fractions.
        bounds = plot.vb.sceneBoundingRect().adjusted(4, 4, -4, -4)
        rect = tip.sceneBoundingRect()
        if rect.width() > bounds.width() or rect.height() > bounds.height():
            tip.hide()  # Full text remains available in the readout's native tooltip.
            self.readout_hover.setToolTip(self.readout_hover.text())
            return
        dx = max(bounds.left() - rect.left(), min(0., bounds.right() - rect.right()))
        dy = max(bounds.top() - rect.top(), min(0., bounds.bottom() - rect.bottom()))
        origin = plot.vb.mapViewToScene(QtCore.QPointF(x_val, y_val))
        tip.setPos(plot.vb.mapSceneToView(origin + QtCore.QPointF(dx, dy)))
        self.readout_hover.setToolTip(self.readout_hover.text())

    @staticmethod
    def _classify_phase_quality(raw_phase, gamma, threshold):
        if not np.isfinite(raw_phase):
            return 'MASKED: phase missing'
        if not np.isfinite(gamma):
            return 'MASKED: same-frame coherence missing'
        if gamma <= 0 or gamma > 1 or abs(raw_phase) > np.pi + 1e-6:
            return 'MASKED: invalid estimate'
        if gamma < threshold:
            return f'MASKED: γ < {threshold:.2f}'
        return 'VALID'

    def scene_mouse_moved(self, pos):
        if self._is_h5():
            return self.h5.mouse_move(pos)
        if self.rolling.hover(pos):return
        threshold = self.threshold.value()

        # 1. Kiểm tra nhóm đồ thị miền Tần số
        for key, plot in self.freq_plots.items():
            if not plot.isVisible() or not plot.vb.sceneBoundingRect().contains(pos):
                continue
            view_pos = plot.vb.mapSceneToView(pos)
            freq_x = float(view_pos.x())
            y_val = float(view_pos.y())
            b = nearest_frequency_bin(freq_x)
            f_bin = START + b * STEP

            # Đồng bộ đường gióng dọc trên cả 4 đồ thị tần số, đường ngang trên đồ thị đang trỏ
            for k, p in self.freq_plots.items():
                if p.isVisible():
                    self.crosshair_v[k].setPos(f_bin)
                    self.crosshair_v[k].setVisible(True)
                    self.crosshair_h[k].setPos(y_val)
                    self.crosshair_h[k].setVisible(k == key)
            for k in self.time_plots:
                self.crosshair_v[k].setVisible(False)
                self.crosshair_h[k].setVisible(False)

            if key == 'spectrum':
                s12_v = self.displayed_streams[S12][b]
                deep_v = self.displayed_deep_db[b]
                s11_v = self.displayed_streams[S11][b]
                s22_v = self.displayed_streams[S22][b]
                s12_str = f'{s12_v:+.2f} dB' if np.isfinite(s12_v) else 'NaN'
                deep_str = f'{deep_v:+.2f} dB' if np.isfinite(deep_v) else 'NaN'
                s11_str = f'{s11_v:+.2f} dB' if np.isfinite(s11_v) else 'NaN'
                s22_str = f'{s22_v:+.2f} dB' if np.isfinite(s22_v) else 'NaN'
                self.readout_hover.setText(
                    f'HOVER [Spectrum 1D]: Bin {b} @ {f_bin:.6f} MHz | '
                    f'|S12|={s12_str} | Mean |S12|={deep_str} | S11={s11_str} | S22={s22_str}'
                )
                tip_text = (
                    f'Bin {b} ({f_bin:.6f} MHz)\n'
                    f'|S12|: {s12_str} | Mean: {deep_str}\n'
                    f'S11: {s11_str} | S22: {s22_str}'
                )
                self._place_tooltip(key, plot, f_bin, y_val, tip_text)
                return

            if key in ('wf_fast', 'wf_phase'):
                row_idx = int(np.floor(y_val))
                row = self._wf_row_at(y_val)
                wf_name = 'WF |S12| Fast' if key == 'wf_fast' else 'WF Phase'
                if row is None:
                    self.readout_hover.setText(
                        f'HOVER [{wf_name}]: Bin {b} @ {f_bin:.6f} MHz | Row {row_idx} | No frame / gap (NaN)'
                    )
                    self._place_tooltip(key, plot, f_bin, y_val, f'Bin {b} ({f_bin:.6f} MHz)\nRow {row_idx}: Gap / Empty')
                    return
                session, fid, stamp, count, streams = row
                s12_arr = streams.get(S12)
                ph_arr = streams.get(PHASE)
                coh_arr = streams.get(COHERENCE)
                s12_v = float(s12_arr[b]) if s12_arr is not None else np.nan
                raw_phase = float(ph_arr[b]) if ph_arr is not None else np.nan
                gamma = float(coh_arr[b]) if coh_arr is not None else np.nan
                s12_str = f'{s12_v:+.2f} dB' if np.isfinite(s12_v) else 'missing'
                ph_str = f'{np.rad2deg(raw_phase):+.2f}° ({raw_phase:+.4f} rad)' if np.isfinite(raw_phase) else 'missing'
                gam_str = f'{gamma:.4f}' if np.isfinite(gamma) else 'missing'
                qual = self._classify_phase_quality(raw_phase, gamma, threshold)
                vn_time = format_vn_time(stamp / 1e6, '%H:%M:%S.%f', milliseconds=True)
                self.readout_hover.setText(
                    f'HOVER [{wf_name}]: Bin {b} @ {f_bin:.6f} MHz | |S12|={s12_str} | '
                    f'Phase={ph_str} | γ={gam_str} | {qual} | Frame {fid} | VN {vn_time}'
                )
                tip_text = (
                    f'VN {vn_time} | Frame {fid}\n'
                    f'Bin {b} ({f_bin:.6f} MHz)\n'
                    f'|S12|: {s12_str} | γ: {gam_str}\n'
                    f'Phase: {ph_str} [{qual}]'
                )
                self._place_tooltip(key, plot, f_bin, y_val, tip_text)
                return

            if key == 'wf_deep':
                group = self._deep_group_at(y_val)
                if group is None:
                    text = 'HOVER [WF Deep]: no completed integration at this row / gap'
                else:
                    session, group_id, first_id, last_id, first_stamp, last_stamp, db = group
                    val = float(db[b])
                    start = format_vn_time(first_stamp/1e6, '%H:%M:%S.%f', milliseconds=True)
                    stop = format_vn_time(last_stamp/1e6, '%H:%M:%S.%f', milliseconds=True)
                    text = (f'HOVER [WF Deep]: Bin {b} @ {f_bin:.6f} MHz | Mean |S12|={val:+.2f} dB | '
                            f'Frames {first_id}–{last_id} | VN {start} → {stop} | group-end timestamp for drift')
                self.readout_hover.setText(text)
                self._place_tooltip(key, plot, f_bin, y_val, text.replace(' | ', '\n'))
                return

        # 2. Kiểm tra nhóm đồ thị miền Thời gian (Phase & Coherence)
        for key, plot in self.time_plots.items():
            if not plot.isVisible() or not plot.vb.sceneBoundingRect().contains(pos):
                continue
            view_pos = plot.vb.mapSceneToView(pos)
            t_x = float(view_pos.x())
            y_val = float(view_pos.y())

            for k in self.freq_plots:
                self.crosshair_v[k].setVisible(False)
                self.crosshair_h[k].setVisible(False)
            for k, p in self.time_plots.items():
                if p.isVisible():
                    self.crosshair_v[k].setPos(t_x)
                    self.crosshair_v[k].setVisible(True)
                    self.crosshair_h[k].setPos(y_val)
                    self.crosshair_h[k].setVisible(k == key)

            if key == 'power':
                x,a,b,c=self.model.power_time_series()
                good=np.flatnonzero(np.isfinite(x))
                if len(good):
                    i=good[np.argmin(abs(x[good]-t_x))]
                    self.readout_hover.setText(f'Power | S11={a[i]:.3f} dB | S22={b[i]:.3f} dB | |S12|={c[i]:.3f} dB | VN {format_vn_time(x[i])}')
                return
            valid_samples = [row for row in self._display_trace if row is not None]
            plot_label = {'phase': 'Phase(t)', 'gamma': 'γ(t)', 'fringe': 'Fringes Re/Im'}[key]
            if not valid_samples:
                self.readout_hover.setText(f'HOVER [{plot_label}]: waiting for selected-bin time trace data')
                self._place_tooltip(key, plot, t_x, y_val, 'Waiting for trace data')
                return

            nearest = min(valid_samples, key=lambda r: abs(r[2] / 1e6 - t_x))
            session, fid, stamp, count, raw_phase, gamma = nearest
            snap_t = stamp / 1e6
            for k, p in self.time_plots.items():
                if p.isVisible():
                    self.crosshair_v[k].setPos(snap_t)

            if key == 'fringe':
                values = self._fringe_values_by_key.get(tuple(nearest[:4]))
                if values is None:
                    text = f'HOVER [Fringes]: frame {fid} | missing same-frame magnitude / phase / coherence'
                else:
                    db, real, imag = values
                    units = 'normalized' if self.fringe_mode.currentData() == 'normalized' else 'ADC-count²/bin'
                    valid = np.isfinite(real) and np.isfinite(imag)
                    quality = 'VALID' if valid else 'MASKED / missing / linear floor'
                    if valid and getattr(self, '_fringe_render_error', False):
                        quality += ' · display range overflow'
                    vn = format_vn_time(snap_t, '%H:%M:%S.%f', milliseconds=True)
                    text = (f'HOVER [Fringes]: VN {vn} | Frame {fid} | '
                            f'Re={real:+.6g} | Im={imag:+.6g} {units} | S12={db:+.3f} dB | {quality}')
                self.readout_hover.setText(text)
                self._place_tooltip(key, plot, snap_t, y_val, text.replace(' | ', '\n'))
                return

            ph_str = f'{np.rad2deg(raw_phase):+.3f}° ({raw_phase:+.5f} rad)' if np.isfinite(raw_phase) else 'missing'
            gam_str = f'{gamma:.4f}' if np.isfinite(gamma) else 'missing'
            qual = self._classify_phase_quality(raw_phase, gamma, threshold)
            vn_time = format_vn_time(snap_t, '%H:%M:%S.%f', milliseconds=True)
            self.readout_hover.setText(
                f'HOVER [{plot_label}]: VN {vn_time} | Frame {fid} | '
                f'Phase={ph_str} | γ={gam_str} | {qual}'
            )
            tip_text = (
                f'VN {vn_time} | Frame {fid}\n'
                f'Phase: {ph_str}\n'
                f'γ: {gam_str} [{qual}]'
            )
            self._place_tooltip(key, plot, snap_t, y_val, tip_text)
            return

        # Chuột nằm ngoài tất cả đồ thị
        self._hide_all_hover_overlays()

    # --- Cập nhật Readout Pinned & Vòng lặp Thu thập / Vẽ Đồ thị ---

    def update_readout(self, threshold):
        sample = next((row for row in reversed(self.model.trace) if row is not None), None)
        if sample is None:
            self.readout_pinned.setText(
                f'PINNED: Bin {self.model.bin} @ {self.model.actual:.6f} MHz — waiting for new data after selection'
            )
            return
        session, fid, stamp, count, raw_phase, gamma = sample
        phase_text = f'{np.rad2deg(raw_phase):+.3f}° ({raw_phase:+.5f} rad)' if np.isfinite(raw_phase) else 'missing'
        gamma_text = f'{gamma:.4f}' if np.isfinite(gamma) else 'missing'
        quality = self._classify_phase_quality(raw_phase, gamma, threshold)
        stamp_text = format_vn_time(stamp / 1e6, '%H:%M:%S.%f', milliseconds=True)
        pause_tag = ' [PAUSED]' if self.pause_button.isChecked() else ''
        self.readout_pinned.setText(
            f'PINNED{pause_tag}: Bin {self.model.bin} @ {self.model.actual:.6f} MHz | '
            f'Received phase: {phase_text} | γ={gamma_text} | {quality} | Frame {fid} | VN {stamp_text}'
        )

    def sync_displayed_curves(self):
        if self._is_h5():
            return
        for kind in (S12, S11, S22):
            self.displayed_streams[kind] = self.latest_streams[kind].copy()
            self.curves[kind].setData(self.freq, self.displayed_streams[kind])
        self.displayed_deep_db = self.latest_deep_db.copy()
        self.deep_curve.setData(self.freq, self.displayed_deep_db)
        self.update_peak_marker_and_metrics()
        if self.autoscale_y_check.isChecked():
            self.fit_spectrum_y()

    def update_data(self):
        reports = []
        incoming, complete = [], []
        for kind, receiver in self.receivers.items():
            frames, stats, skips, error = receiver.take()
            reports.append(
                f'{kind}:complete={stats.get("complete",0)} invalid={stats.get("invalid",0)} skips={skips}'
                + (f' ERROR {error}' if error else '')
            )
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
            self._push_deep_observation(session, fid, stamp, count, streams[S12])
            for kind in (S12, S11, S22):
                self.latest_streams[kind] = np.asarray(
                    streams.get(kind, np.full(BINS, np.nan, dtype=np.float32)), dtype=np.float32
                )

        if complete and not self.pause_button.isChecked() and not self._is_h5():
            self.sync_displayed_curves()
            self.render()

        pause_state = ' | DISPLAY PAUSED' if self.pause_button.isChecked() else ''
        self.statusBar().showMessage(
            ' | '.join(reports) + f' | join cache={len(self.joiner.pending)}/{self.joiner.capacity}{pause_state}'
        )

    def render(self, *_):
        if self._is_h5():
            return self.h5.render()
        if self.pause_button.isChecked():
            return
        self._display_rows = tuple(self.model.rows)
        self._display_trace = tuple(self.model.trace)
        self._display_deep_rows = tuple(self.deep_history)
        self._advance_wf_coordinates()
        for plot in (self.wf_fast_plot, self.wf_phase_plot, self.wf_deep_plot):
            plot.getAxis('left').display_rows = self._display_rows
            plot.getAxis('left').display_end = self._wf_display_end
        self._update_wf_view()
        threshold = self.threshold.value()
        self.mask_line.setValue(threshold)
        x, phase, _ = self.model.time_series(threshold)
        self.phase_curve.setData([], [])  # Raw phase is shown as samples, without interpolation.
        good = np.isfinite(x) & np.isfinite(phase)
        self.phase_points.setData(x[good], phase[good])
        self.readout_pinned.setToolTip(self.readout_pinned.text())
        coherence_x, gamma = coherence_time_series(self._display_trace)
        self.gamma_curve.setData(coherence_x, gamma, connect='finite')
        self._update_fringe_plot()
        self._update_power_plot()
        self.update_readout(threshold)
        self._update_dashboard_metrics(threshold)
        self.update_time_view()
        self._render_wf_window()
        if hasattr(self,'rolling'):self.rolling.style()
        self.wf_phase_plot.setTitle(f'Phase ° — display mask γ ≥ {threshold:.2f}; missing/invalid = transparent')
        latest = next((row for row in reversed(self.model.rows) if row is not None), None)
        stamp = (
            format_vn_time(latest[2] / 1e6, '%Y-%m-%d %H:%M:%S.%f', milliseconds=True)
            if latest
            else 'waiting'
        )
        self.phase_plot.setTitle(f'Bin {self.model.bin}, {self.model.actual:.6f} MHz — wrapped phase | VN {stamp}')
        # Timestamp labels refer to the current ring contents, not cached ticks.
        for plot in (self.wf_fast_plot, self.wf_phase_plot, self.wf_deep_plot):
            plot.getAxis('left').picture = None
            plot.getAxis('left').update()

    def closeEvent(self, event):
        if not self.rolling.close():
            event.ignore();QtCore.QTimer.singleShot(100,self.close);return
        if self.h5.worker is not None:
            self.h5.cancel()
            event.ignore()
            QtCore.QTimer.singleShot(100, self.close)
            return
        if self._persist_settings and not self._is_h5():
            self._save_ui_settings()
        self.timer.stop()
        for receiver in self.receivers.values():
            receiver.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description='Typed SOL2 v3 CW / Solar viewer; legacy v2 is rejected')
    parser.add_argument('--source', choices=('live', 'h5'), default='live')
    parser.add_argument('--offline', action='store_true', help='Alias for --source h5; same viewer window')
    parser.add_argument('--h5', nargs='*', default=[], help='Closed H5 files to open in this viewer')
    parser.add_argument('files', nargs='*', help='Optional H5 file paths (same viewer)')
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
    parser.add_argument(
        '--coherence-threshold', type=float, default=.5, help='Adjustable display mask, not an acceptance criterion'
    )
    parser.add_argument('--deep-frames', type=int, default=5)
    parser.add_argument('--source-ip')
    parser.add_argument('--time-window', type=int, default=120, help='Live phase/coherence window in seconds (15..1800)')
    args = parser.parse_args()
    if args.port_aux is not None or args.aux_mode is not None:
        parser.error('SOL2 v3: replace --port-aux/--aux-mode with --port-phase and --port-coherence')
    ports = {
        S12: args.port_s12,
        S11: args.port_s11,
        S22: args.port_s22,
        PHASE: args.port_phase,
        COHERENCE: args.port_coherence,
    }
    if not 1 <= args.port_s12 <= 65535 or any(not 0 <= p <= 65535 for p in ports.values()):
        parser.error('S12 port must be 1..65535; optional ports 0..65535')
    active = [p for p in ports.values() if p]
    if len(active) != len(set(active)):
        parser.error('Enabled ports must be distinct')
    if not np.isfinite(args.frequency_mhz) or not START <= args.frequency_mhz <= STOP - STEP:
        parser.error('Frequency must be inside the sampled bin range')
    if not 15 <= args.time_window <= 1800:
        parser.error('time-window must be 15..1800 seconds')
    if not 0 <= args.coherence_threshold <= 1 or not 1 <= args.deep_frames <= HISTORY:
        parser.error('Threshold must be 0..1 and deep-frames 1..300')
    app = QtWidgets.QApplication(sys.argv[:1])
    pg.setConfigOptions(antialias=False, imageAxisOrder='row-major')
    receivers = {}
    def receiver_factory():
        created = {}
        try:
            for kind, port in ports.items():
                if port:
                    created[kind] = Receiver(args.bind, port, args.source_ip, expected_type=kind)
            for receiver in created.values(): receiver.start()
            receivers.update(created)
            return created
        except Exception:
            for receiver in created.values(): receiver.close()
            raise
    try:
        h5_paths = args.h5 + args.files
        h5_source = args.source == 'h5' or args.offline or bool(h5_paths)
        initial = {} if h5_source else receiver_factory()
        window = FXViewer(
            initial, args.mode, args.deep_frames, args.frequency_mhz,
            args.coherence_threshold, args.time_window,
            receiver_factory=receiver_factory,
        )
        if h5_source:
            window.h5.source.setCurrentIndex(1)
            if h5_paths:
                QtCore.QTimer.singleShot(0, lambda: window.h5.open(h5_paths))
        signal.signal(signal.SIGINT, lambda *_: window.close())
        signal.signal(signal.SIGTERM, lambda *_: window.close())
        window.show()
        return app.exec_()
    finally:
        for receiver in receivers.values():
            receiver.close()


if __name__ == '__main__':
    sys.exit(main())
