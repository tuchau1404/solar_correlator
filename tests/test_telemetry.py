#!/usr/bin/env python3
import os
import pathlib
import socket
import subprocess
import sys
import time
import unittest
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'viewer'))
from telemetry_protocol import HEADER, Reassembler, Receiver

PEER = ('127.0.0.1', 45678)

def packets(frame=0, session=123):
    values = np.arange(2048, dtype=np.float32)
    return [HEADER.pack(b'SOL2', 2, 52, session, frame, 1700000000000000,
                        1953, 1, 2048, i, 8, 256, i * 256)
            + values[i * 256:(i + 1) * 256].astype('>f4').tobytes() for i in range(8)]


class ProtocolTests(unittest.TestCase):
    def test_reverse_and_duplicate(self):
        r = Reassembler()
        p = packets()
        self.assertIsNone(r.feed(p[7], PEER, 0))
        self.assertIsNone(r.feed(p[7], PEER, 0.01))
        for part in reversed(p[:7]):
            result = r.feed(part, PEER, 0.02)
        np.testing.assert_array_equal(result[4], np.arange(2048, dtype=np.float32))
        self.assertEqual(r.stats['duplicate'], 1)

    def test_loss_expiry_and_capacity(self):
        r = Reassembler()
        for frame in range(40):
            r.feed(packets(frame)[0], PEER, frame * 0.001)
        self.assertLessEqual(len(r.pending), 8)
        r.expire(1)
        self.assertEqual(len(r.pending), 0)
        self.assertEqual(r.stats['incomplete'], 40)

    def test_invalid_metadata_and_nonfinite(self):
        r = Reassembler()
        p = packets()[0]
        for corrupt in (p[:-1], p + b'x', b'BAD!' + p[4:], p[:52] + np.full(256, np.nan, dtype='>f4').tobytes()):
            self.assertIsNone(r.feed(corrupt, PEER, 0))
        self.assertEqual(r.stats['invalid'], 4)
        self.assertFalse(r.pending)

    def test_gap_late_restart(self):
        r = Reassembler()
        for f in (0, 2):
            for p in packets(f):
                result = r.feed(p, PEER, f * 0.1)
            self.assertIsNotNone(result)
        self.assertEqual(r.stats['frame_gaps'], 1)
        self.assertIsNone(r.feed(packets(1)[0], PEER, 0.3))
        self.assertIsNone(r.feed(packets(0, 456)[0], PEER, 0.4))
        for p in packets(0, 456):
            result = r.feed(p, PEER, 1.5)
        self.assertEqual(result[0], 456)
        self.assertEqual(r.stats['restarts'], 1)

    def test_cpp_loopback(self):
        binary = os.environ.get('SENDER_BINARY')
        if not binary:
            self.skipTest('Set SENDER_BINARY for C++ loopback integration')
        receiver = Receiver('127.0.0.1', 0)
        port = receiver.sock.getsockname()[1]
        receiver.start()
        try:
            run = subprocess.run([binary, '127.0.0.1', str(port), '6'], capture_output=True, text=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            time.sleep(0.1)
            frames, stats, skips, error = receiver.take()
            self.assertIsNone(error)
            self.assertEqual(len(frames), 6)
            self.assertEqual(stats['complete'], 6)
            stamps = np.array([frame[2] for frame in frames], dtype=np.int64)
            self.assertLess(abs(stamps[-1] - stamps[0] - 1000000), 100000)
            self.assertLess(abs(stamps[-1] / 1e6 - time.time()), 2)
            for _, _, _, count, values in frames:
                self.assertEqual(count, 1953)
                self.assertTrue(np.isfinite(values).all())
                self.assertGreater(values.min(), 20)
                self.assertLess(values.max(), 85)
        finally:
            receiver.close()


if __name__ == '__main__':
    unittest.main()
