"""Bounded SOL2 reassembly and a GUI-independent socket worker (Python 3)."""
import collections
import select
import socket
import struct
import threading
import time
import numpy as np

HEADER = struct.Struct('!4sHHQQQIIHHHHI')
BINS, CHUNK_BINS, CHUNKS = 2048, 256, 8
PACKET_SIZE = HEADER.size + CHUNK_BINS * 4


class Reassembler:
    def __init__(self, source_ip=None):
        self.source_ip = source_ip
        self.pending = {}
        self.active = None
        self.last_activity = 0.0
        self.last_id = None
        self.stats = collections.Counter()

    def expire(self, now):
        for key, state in list(self.pending.items()):
            if now - state[0] > 0.75:
                del self.pending[key]
                self.stats['incomplete'] += 1

    def feed(self, data, peer, now=None):
        now = time.monotonic() if now is None else now
        self.expire(now)
        if self.source_ip and peer[0] != self.source_ip:
            self.stats['foreign'] += 1
            return None
        if len(data) != PACKET_SIZE:
            self.stats['invalid'] += 1
            return None
        magic, version, size, session, frame, stamp, count, flags, bins, idx, chunks, n, offset = HEADER.unpack_from(data)
        if (magic != b'SOL2' or version != 2 or size != HEADER.size or not session
                or count != 1953 or flags != 1 or bins != BINS or chunks != CHUNKS
                or idx >= CHUNKS or n != CHUNK_BINS or offset != idx * CHUNK_BINS):
            self.stats['invalid'] += 1
            return None
        values = np.frombuffer(data, dtype='>f4', count=n, offset=HEADER.size)
        if not np.isfinite(values).all():
            self.stats['invalid'] += 1
            return None
        stream = (peer, session)
        if stream != self.active:
            # One source at a time. After a one-second outage allow a new session.
            if self.active is not None and now - self.last_activity < 1.0:
                self.stats['foreign'] += 1
                return None
            if self.active is not None:
                self.stats['restarts'] += 1
                self.stats['incomplete'] += len(self.pending)
            self.pending.clear()
            self.active, self.last_id = stream, None
        self.last_activity = now
        if self.last_id is not None and frame <= self.last_id:
            self.stats['late'] += 1
            return None
        metadata = (stamp, count, flags)
        if frame not in self.pending:
            if len(self.pending) >= 8:
                oldest = min(self.pending, key=lambda k: self.pending[k][0])
                del self.pending[oldest]
                self.stats['incomplete'] += 1
            self.pending[frame] = [now, metadata, 0, np.empty(BINS, dtype=np.float32)]
        state = self.pending[frame]
        if state[1] != metadata:
            self.stats['invalid'] += 1
            return None
        mask = 1 << idx
        if state[2] & mask:
            self.stats['duplicate'] += 1
            if not np.array_equal(state[3][offset:offset + n], values):
                del self.pending[frame]
                self.stats['invalid'] += 1
            return None
        state[3][offset:offset + n] = values
        state[2] |= mask
        if state[2] != (1 << CHUNKS) - 1:
            return None
        del self.pending[frame]
        if self.last_id is not None:
            self.stats['frame_gaps'] += frame - self.last_id - 1
        self.last_id = frame
        # Latest complete frame wins; late completion is a distinct discard policy.
        for old in list(self.pending):
            if old < frame:
                del self.pending[old]
                self.stats['incomplete'] += 1
        self.stats['complete'] += 1
        return (session, frame, stamp, count, state[3])


class Receiver(threading.Thread):
    def __init__(self, bind='0.0.0.0', port=9999, source_ip=None):
        super().__init__(name='udp-receiver', daemon=False)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            self.actual_rcvbuf = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            self.sock.bind((bind, port))
            self.sock.setblocking(False)
        except BaseException:
            self.sock.close()
            raise
        self.decoder = Reassembler(source_ip)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.frames = collections.deque(maxlen=300)
        self.display_skips = 0
        self.snapshot = {}
        self.error = None

    def run(self):
        try:
            while not self.stop_event.is_set():
                ready, _, _ = select.select([self.sock], [], [], 0.05)
                self.decoder.expire(time.monotonic())
                if ready:
                    # Bound each drain so shutdown remains responsive under a flood.
                    for _ in range(512):
                        if self.stop_event.is_set():
                            break
                        try:
                            data, peer = self.sock.recvfrom(65535)
                        except BlockingIOError:
                            break
                        frame = self.decoder.feed(data, peer)
                        if frame is not None:
                            with self.lock:
                                if len(self.frames) == self.frames.maxlen:
                                    self.display_skips += 1
                                self.frames.append(frame)
                with self.lock:
                    self.snapshot = dict(self.decoder.stats)
        except OSError as exc:
            with self.lock:
                self.error = str(exc)
        finally:
            self.sock.close()

    def take(self):
        with self.lock:
            frames = list(self.frames)
            self.frames.clear()
            return frames, dict(self.snapshot), self.display_skips, self.error

    def close(self):
        self.stop_event.set()
        if self.ident is not None:
            self.join()
        else:
            self.sock.close()
