import pathlib
import sys
import unittest
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'viewer'))
from telemetry_protocol import HEADER, Reassembler, FrameJoiner, S12, S11, S22, PHASE, COHERENCE
PEER = ('127.0.0.1', 5000)

def packets(kind=S12, frame=0, session=123, stamp=1700000000000000, values=None):
    if values is None:
        values = np.full(2048, .5, dtype=np.float32)
    return [HEADER.pack(b'SOL2', 3, 52, session, frame, stamp, 1953,
                        1 | (kind << 8), 2048, i, 8, 256, i * 256)
            + values[i*256:(i+1)*256].astype('>f4').tobytes() for i in range(8)]

def frame(fid=0, session=123, stamp=100, value=.5):
    return session, fid, stamp, 1953, np.full(2048, value, dtype=np.float32)

class TypedProtocolTests(unittest.TestCase):
    def test_five_types_reverse_duplicate(self):
        joined = FrameJoiner()
        for kind in (COHERENCE, PHASE, S22, S11, S12):
            r = Reassembler(expected_type=kind)
            parts = packets(kind)
            r.feed(parts[-1], PEER, 0)
            r.feed(parts[-1], PEER, .01)
            for p in reversed(parts[:-1]):
                result = r.feed(p, PEER, .02)
            joined.add(kind, result, .02)
            self.assertEqual(r.stats['duplicate'], 1)
        output = joined.poll(.03)
        self.assertEqual(len(output), 1)
        self.assertEqual(set(output[0][4]), set(range(1, 6)))
        self.assertEqual(output[0][:4], (123, 0, 1700000000000000, 1953))

    def test_version_type_and_range_rejection(self):
        r = Reassembler(expected_type=PHASE)
        self.assertIsNone(r.feed(packets(COHERENCE)[0], PEER, 0))
        p = bytearray(packets(PHASE)[0]); p[5] = 2
        self.assertIsNone(r.feed(p, PEER, 0))
        self.assertEqual(r.stats['unsupported_type'], 1)
        self.assertEqual(r.stats['unsupported_version'], 1)
        self.assertIn('expected 3', r.last_error)
        for kind, val in ((PHASE, 4), (COHERENCE, 1.1)):
            decoder = Reassembler(expected_type=kind)
            decoder.feed(packets(kind, values=np.full(2048, val))[0], PEER, 0)
            self.assertEqual(decoder.stats['invalid'], 1)

    def test_chunk_loss_timeout_bound(self):
        r = Reassembler(expected_type=S12)
        for fid in range(40):
            r.feed(packets(frame=fid)[0], PEER, .001*fid)
        self.assertLessEqual(len(r.pending), 8)
        r.expire(1)
        self.assertFalse(r.pending)
        self.assertEqual(r.stats['incomplete'], 40)

    def test_join_missing_no_old_coherence(self):
        j = FrameJoiner(timeout=.3)
        for kind in (S12, PHASE, COHERENCE):
            j.add(kind, frame(0), 0)
        j.add(S12, frame(1, stamp=200), .1)
        j.add(PHASE, frame(1, stamp=200), .1)
        out = j.poll(.5)
        self.assertEqual([f[1] for f in out], [0, 1])
        self.assertNotIn(COHERENCE, out[1][4])
        self.assertIn(S12, out[1][4])

    def test_metadata_mismatch_restart_and_capacity(self):
        j = FrameJoiner(enabled=(S12, PHASE, COHERENCE))
        j.add(S12, frame(0), 0)
        j.add(PHASE, frame(0, stamp=101), 0)
        j.add(COHERENCE, frame(0), 0)
        self.assertNotIn(PHASE, j.poll(.01)[0][4])
        j.add(COHERENCE, frame(0, session=456), .1)
        j.add(S12, frame(0, session=456), .1)
        j.add(PHASE, frame(0, session=456), .1)
        self.assertEqual(j.poll(.2)[0][0], 456)
        j.add(S12, frame(10), .3)
        self.assertEqual(j.active, 456)
        for fid in range(500):
            j.add(PHASE, frame(fid, session=456), .4)
        self.assertLessEqual(len(j.pending), j.capacity)
        j.poll(2)
        self.assertFalse(j.pending)

    def test_reordered_complete_frames_restart_and_conflicting_chunk(self):
        r = Reassembler(expected_type=PHASE)
        parts0, parts1 = packets(PHASE, frame=0), packets(PHASE, frame=1)
        r.feed(parts0[0], PEER, 0)
        for p in parts1:
            result = r.feed(p, PEER, .01)
        self.assertEqual(result[1], 1)
        for p in parts0[1:]:
            result = r.feed(p, PEER, .02)
        self.assertEqual(result[1], 0)
        for p in packets(PHASE, session=456):
            result = r.feed(p, PEER, .03)
        self.assertEqual(result[0], 456)
        self.assertIsNone(r.feed(parts1[0], PEER, .04))
        p = packets(PHASE, frame=2, session=456)[0]
        r.feed(p, PEER, .05)
        conflict = p[:52] + np.full(256, .6, dtype='>f4').tobytes()
        r.feed(conflict, PEER, .06)
        self.assertEqual(r.stats['invalid'], 1)
        self.assertNotIn(2, r.pending)

    def test_legacy_is_explicit_and_session_isolation(self):
        r = Reassembler()  # legacy decoder only
        self.assertIsNone(r.feed(packets()[0], PEER, 0))
        self.assertEqual(r.stats['unsupported_version'], 1)
        j = FrameJoiner(enabled=(S12, PHASE, COHERENCE))
        j.add(S12, frame(3), 0)
        j.add(PHASE, frame(3), 0)
        j.add(COHERENCE, frame(3, session=456), 0)
        result = j.poll(1)
        self.assertNotIn(COHERENCE, result[0][4])

if __name__ == '__main__':
    unittest.main()
