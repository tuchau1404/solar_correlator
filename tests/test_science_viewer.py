"""Run: QT_QPA_PLATFORM=offscreen python tests/test_science_viewer.py."""
import pathlib
import sys
import unittest
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'viewer'))
from telemetry_protocol import S12, S11, S22, PHASE, COHERENCE
from science_model import ObservationHistory, masked_phase, BINS, PERIOD
from viewer_fx import FXViewer, QtWidgets, QtCore
from PyQt5 import QtTest

STAMP = 1700000000000000

def observation(fid, phase=30., gamma=.9, session=1, bin=1126, burst=False):
    rng = np.random.default_rng(fid)
    angles = rng.uniform(-np.pi, np.pi, BINS).astype(np.float32)
    angles[bin] = np.deg2rad(phase)
    coherence = np.full(BINS, .05, dtype=np.float32); coherence[bin] = gamma
    power = np.full(BINS, 25., dtype=np.float32); power[bin] = 70
    streams = {S12: power, S11: power+1, S22: power+2, PHASE: angles, COHERENCE: coherence}
    return session, fid, STAMP+round(fid*PERIOD*1e6), 1953, streams

class HistoryTests(unittest.TestCase):
    def test_fixed_cw_and_noise_mask(self):
        m = ObservationHistory()
        for fid in range(30):
            m.push(observation(fid))
        x, y, gamma = m.time_series(.5)
        np.testing.assert_allclose(y, 30., atol=1e-5)
        np.testing.assert_allclose(gamma, .9, atol=1e-6)
        np.testing.assert_allclose(x, [ (STAMP+round(i*PERIOD*1e6))/1e6 for i in range(30)])
        image = m.image(PHASE, .5)
        self.assertEqual(np.isfinite(image).sum(), 30)
        self.assertEqual(m.bin, 1126)

    def test_wrap_gap_restart_bin_change(self):
        m = ObservationHistory()
        m.push(observation(0, phase=179)); m.push(observation(1, phase=-179))
        x, y, _ = m.time_series(.5)
        self.assertTrue(np.isnan(y[1]))
        np.testing.assert_allclose(y[[0,2]], [179, -179], atol=1e-4)
        m.push(observation(3, phase=-178))
        self.assertTrue(np.isnan(m.image(S12)[-2]).all())
        self.assertTrue(np.isnan(m.time_series(.5)[1][-2]))
        m.select(36)
        self.assertEqual(len(m.time_series(.5)[0]), 0)
        self.assertEqual(len(m.rows), 4)
        m.push(observation(0, session=2))
        self.assertEqual(len(m.rows), 1)
        self.assertEqual(len(m.trace), 1)

    def test_no_coherence_invalid_never_zero(self):
        m = ObservationHistory(); obs = observation(0)
        del obs[4][COHERENCE]; m.push(obs)
        self.assertTrue(np.isnan(m.image(PHASE)).all())
        self.assertEqual(np.isfinite(m.image(S12)).sum(), BINS)
        zero = observation(1, phase=0, gamma=0)
        self.assertTrue(np.isnan(masked_phase(zero[4], 0)[1126]))
        valid = observation(2, phase=0, gamma=.9)
        self.assertEqual(masked_phase(valid[4], .5)[1126], 0)

    def test_drifting_burst_known_phase_and_bounds(self):
        m = ObservationHistory(history=16)
        for fid in range(200):
            bin = 1000+fid
            m.push(observation(fid, phase=-60+fid/10, bin=bin))
        self.assertEqual(len(m.rows), 16)
        self.assertEqual(len(m.trace), 16)
        amplitude, phase = m.image(S12), m.image(PHASE, .5)
        for index, fid in enumerate(range(184, 200)):
            peak = int(np.nanargmax(amplitude[index]))
            self.assertEqual(peak, 1000+fid)
            self.assertAlmostEqual(phase[index, peak], -60+fid/10, places=4)
        self.assertEqual(np.isfinite(phase).sum(), 16)

class FakeReceiver:
    def __init__(self): self.frames = []; self.closed = False
    def take(self):
        frames, self.frames = self.frames, []
        return frames, {}, 0, None
    def close(self): self.closed = True

class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_offscreen_modes_geometry_mask_levels_and_select(self):
        receivers = {kind: FakeReceiver() for kind in range(1, 6)}
        w = FXViewer(receivers, mode='solar')
        w.timer.stop()
        try:
            for fid in range(80):
                obs = observation(fid)
                for kind in receivers:
                    receivers[kind].frames.append((*obs[:4], obs[4][kind]))
            w.update_data(); w.show(); self.app.processEvents()
            self.assertEqual(len(w.model.rows), 80)
            self.assertAlmostEqual(w.spectrum_plot.viewRange()[0][0], 30-10/2048/2)
            self.assertAlmostEqual(w.spectrum_plot.viewRange()[0][1], 40-10/2048/2)
            self.assertEqual(np.isfinite(w.img_phase.image).sum(), 80)
            w.img_phase.render()
            self.assertEqual(w.img_phase.qimage.pixelColor(0, 299).alpha(), 0)
            self.assertEqual(w.img_phase.qimage.pixelColor(1126, 299).alpha(), 255)
            p1 = w.img_fast.mapToParent(QtCore.QPointF(1126.5, 299.5))
            p2 = w.img_phase.mapToParent(QtCore.QPointF(1126.5, 299.5))
            self.assertEqual(p1, p2)
            self.assertAlmostEqual(p1.x(), w.model.actual)
            self.assertEqual(w.cbar_phase.colorMap().getColors()[0].tolist(), w.cbar_phase.colorMap().getColors()[-1].tolist())
            w.cbar_fast.setLevels((10, 70)); w.threshold.setValue(.95)
            self.assertTrue(np.isnan(w.img_phase.image).all())
            self.assertEqual(tuple(w.img_fast.getLevels()), (10, 70))
            w.frequency.setValue(36)
            self.assertEqual(len(w.model.trace), 0)
            self.assertIn('Bin', w.bin_label.text())
            position = w.spectrum_plot.vb.mapViewToScene(QtCore.QPointF(37, 40))
            QtTest.QTest.mouseClick(w.graphics.viewport(), QtCore.Qt.LeftButton,
                                   pos=w.graphics.mapFromScene(position))
            self.app.processEvents()
            self.assertAlmostEqual(w.frequency.value(), 37, delta=.02)
            w.mode.setCurrentText('CW'); self.app.processEvents()
            self.assertFalse(w.wf_phase_plot.isVisible())
            w.mode.setCurrentText('Solar'); self.app.processEvents()
            self.assertTrue(w.wf_phase_plot.isVisible())
        finally:
            w.close()
        self.assertTrue(all(r.closed for r in receivers.values()))

    def test_missing_coherence_s12_progresses(self):
        receivers = {S12: FakeReceiver(), PHASE: FakeReceiver(), COHERENCE: FakeReceiver()}
        w = FXViewer(receivers)
        w.timer.stop()
        try:
            obs = observation(0)
            for kind in (S12, PHASE):
                w.joiner.add(kind, (*obs[:4], obs[4][kind]), 0)
            w.update_data()
            self.assertEqual(w.model.last_id, 0)
            self.assertTrue(np.isfinite(w.img_fast.image[-1]).all())
            self.assertTrue(np.isnan(w.img_phase.image[-1]).all())
        finally:
            w.close()

if __name__ == '__main__': unittest.main()
