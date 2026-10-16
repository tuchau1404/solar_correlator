"""C++ -> Linux UDP loopback -> Python reassembly/join -> offscreen GUI."""
import os
import pathlib
import subprocess
import sys
import time
import unittest
import numpy as np
sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / 'viewer')]
from telemetry_protocol import Receiver, FrameJoiner, S12, PHASE, COHERENCE
from viewer_fx import FXViewer, QtWidgets

@unittest.skipUnless(os.environ.get('V3_SENDER_BINARY'), 'Set V3_SENDER_BINARY to build/send_v3_fixture')
class UdpV3Tests(unittest.TestCase):
    def test_cw_burst_restart_and_gui(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        receivers = {t: Receiver('127.0.0.1', 0, expected_type=t) for t in range(1,6)}
        window = FXViewer(receivers, mode='solar'); window.timer.stop()
        sessions = []
        try:
            for r in receivers.values(): r.start()
            for mode in ('cw', 'burst'):
                run = subprocess.run([os.environ['V3_SENDER_BINARY'], '127.0.0.1',
                                      *[str(r.sock.getsockname()[1]) for r in receivers.values()], mode],
                                     capture_output=True, text=True, timeout=5)
                self.assertEqual(run.returncode, 0, run.stderr)
                deadline = time.monotonic()+2
                while time.monotonic() < deadline:
                    window.update_data(); app.processEvents()
                    if window.model.last_id == 7 and (not sessions or window.model.session != sessions[-1]):
                        break
                self.assertEqual(window.model.last_id, 7)
                sessions.append(window.model.session)
                self.assertEqual(len(window.model.rows), 8)
                for f, observation in enumerate(window.model.rows):
                    session, fid, stamp, count, streams = observation
                    self.assertEqual(fid, f)
                    self.assertEqual(count, 1953)
                    self.assertEqual(set(streams), set(range(1,6)))
                    self.assertLess(abs(stamp/1e6-time.time()), 3)
                    bin = int(np.argmax(streams[S12]))
                    self.assertEqual(bin, 1126 if mode == 'cw' else 1000+f*4)
                    expected = np.pi/6 if mode == 'cw' else -.8+.05*f
                    self.assertAlmostEqual(float(streams[PHASE][bin]), expected, places=6)
                    self.assertAlmostEqual(float(streams[COHERENCE][bin]), .95, places=6)
                self.assertEqual(np.isfinite(window.img_phase.image).sum(), 8)
                if mode == 'cw':
                    np.testing.assert_allclose(window.model.time_series(.5)[1], 30, atol=1e-4)
            self.assertNotEqual(*sessions)
            window.show(); app.processEvents()
            self.assertTrue(window.wf_phase_plot.isVisible())
        finally:
            window.close()
        self.assertTrue(all(not r.is_alive() for r in receivers.values()))

if __name__ == '__main__': unittest.main()
