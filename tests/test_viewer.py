#!/usr/bin/env python3
"""Run with QT_QPA_PLATFORM=offscreen python3 tests/test_viewer.py."""
import pathlib
import sys
import unittest
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'viewer'))
from viewer import Viewer, Receiver, QtWidgets, QtCore


class GuiTest(unittest.TestCase):
    def test_geometry_levels_and_gaps(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        receiver = Receiver('127.0.0.1', 0)
        window = Viewer(receiver)
        try:
            values = np.full(2048, 25, dtype=np.float32)
            values[1024] = 78
            with receiver.lock:
                receiver.frames.extend([(1, 0, 1, 1953, values), (1, 2, 2, 1953, values)])
            window.update_data()
            window.show()
            app.processEvents()
            self.assertEqual(window.image.image.shape, (300, 2048))
            self.assertTrue(np.isnan(window.display[-2]).all())
            center = window.image.mapToParent(QtCore.QPointF(1024.5, 299.5))
            self.assertAlmostEqual(center.x(), 35)
            top_x = window.spectrum.vb.mapViewToScene(QtCore.QPointF(35, 78)).x()
            bottom_x = window.waterfall.vb.mapViewToScene(center).x()
            self.assertLess(abs(top_x - bottom_x), 1)
            window.colorbar.setLevels((30, 80))
            with receiver.lock:
                receiver.frames.append((1, 3, 3, 1953, values))
            window.update_data()
            self.assertEqual(tuple(window.image.getLevels()), (30, 80))
        finally:
            window.close()


if __name__ == '__main__':
    unittest.main()
