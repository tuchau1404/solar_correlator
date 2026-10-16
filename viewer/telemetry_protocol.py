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
S12, S11, S22, PHASE, COHERENCE = range(1, 6)
PACKET_SIZE = HEADER.size + CHUNK_BINS * 4


class Reassembler:
    def __init__(self, source_ip=None, expected_type=None):
        if expected_type is not None and expected_type not in (S12, S11, S22, PHASE, COHERENCE):
            raise ValueError(f"Unsupported payload type {expected_type}")
        self.source_ip = source_ip
        self.expected_type = expected_type
        self.last_error = None
        self.retired = collections.deque(maxlen=32)
        self.completed = collections.deque(maxlen=64)
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
        expected_version = 2 if self.expected_type is None else 3
        if version != expected_version:
            self.stats['unsupported_version'] += 1
            self.last_error = f'Unsupported SOL2 version {version}; expected {expected_version}'
            return None
        expected_flags = 1 if version == 2 else 1 | (self.expected_type << 8)
        if flags != expected_flags:
            self.stats['unsupported_type'] += 1
            self.last_error = f'Unsupported/wrong payload flags {flags:#x}; expected {expected_flags:#x}'
            return None
        if (magic != b'SOL2' or size != HEADER.size or not session
                or count != 1953 or bins != BINS or chunks != CHUNKS
                or idx >= CHUNKS or n != CHUNK_BINS or offset != idx * CHUNK_BINS):
            self.stats['invalid'] += 1
            return None
        values = np.frombuffer(data, dtype='>f4', count=n, offset=HEADER.size)
        if (not np.isfinite(values).all()
                or (self.expected_type == COHERENCE and ((values < 0).any() or (values > 1).any()))
                or (self.expected_type == PHASE and (np.abs(values) > np.pi + 1e-6).any())):
            self.stats['invalid'] += 1
            return None
        stream = (peer, session)
        if version == 3 and session in self.retired:
            self.stats['late'] += 1
            return None
        if stream != self.active:
            # Legacy waits for an outage; typed v3 switches session immediately.
            if self.active is not None and (version == 2 and now - self.last_activity < 1.0
                    or version == 3 and session == self.active[1]):
                self.stats['foreign'] += 1
                return None
            if self.active is not None:
                if version == 3:
                    self.retired.append(self.active[1])
                self.completed.clear()
                self.stats['restarts'] += 1
                self.stats['incomplete'] += len(self.pending)
            self.pending.clear()
            self.active, self.last_id = stream, None
        self.last_activity = now
        if (version == 2 and self.last_id is not None and frame <= self.last_id
                or version == 3 and (frame in self.completed or self.last_id is not None and frame < self.last_id - 8)):
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
                if version == 3:
                    self.completed.append(frame)  # quarantine a conflicting duplicate
                self.stats['invalid'] += 1
            return None
        state[3][offset:offset + n] = values
        state[2] |= mask
        if state[2] != (1 << CHUNKS) - 1:
            return None
        del self.pending[frame]
        if self.last_id is not None and frame > self.last_id:
            self.stats['frame_gaps'] += frame - self.last_id - 1
        self.last_id = frame if self.last_id is None else max(frame, self.last_id)
        self.completed.append(frame)
        if version == 2:
            # Explicit legacy latest-complete policy. v3 allows reordered frames.
            for old in list(self.pending):
                if old < frame:
                    del self.pending[old]
                    self.stats['incomplete'] += 1
        self.stats['complete'] += 1
        return (session, frame, stamp, count, state[3])


class Receiver(threading.Thread):
    def __init__(self, bind='0.0.0.0', port=9999, source_ip=None, expected_type=None):
        super().__init__(name='udp-receiver', daemon=False)
        self.decoder = Reassembler(source_ip, expected_type)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            self.actual_rcvbuf = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            self.sock.bind((bind, port))
            self.sock.setblocking(False)
        except BaseException:
            self.sock.close()
            raise
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
                    self.error = self.decoder.last_error
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


class FrameJoiner:
    """Bounded session/frame join, anchored by S12, with a nonblocking grace period.

    Streams must already have passed typed v3 decoding. An emitted frame is final;
    late AUX is discarded, never substituted into another observation.
    """
    def __init__(self, enabled=(S12, S11, S22, PHASE, COHERENCE), timeout=0.35, capacity=32):
        self.enabled = set(enabled)
        self.timeout, self.capacity = timeout, capacity
        self.pending = collections.OrderedDict()
        self.active = None
        self.retired = collections.deque(maxlen=32)
        self.last_id = None
        self.stats = collections.Counter()

    def add(self, kind, frame, now=None):
        now = time.monotonic() if now is None else now
        session, fid, stamp, count, values = frame
        if session in self.retired or kind not in self.enabled:
            self.stats['late'] += 1
            return
        if kind == S12 and session != self.active:
            if self.active is not None:
                self.retired.append(self.active)
            self.active, self.last_id = session, None
            self.pending = collections.OrderedDict((k, v) for k, v in self.pending.items() if k[0] == session)
        if session == self.active and self.last_id is not None and fid <= self.last_id:
            self.stats['late'] += 1
            return
        key = session, fid
        state = self.pending.setdefault(key, [now, {}])
        if kind in state[1]:
            self.stats['duplicate'] += 1
            return
        state[1][kind] = (stamp, count, values)
        while len(self.pending) > self.capacity:
            self.pending.popitem(last=False)
            self.stats['evicted'] += 1

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        result = []
        for key in sorted(list(self.pending)):
            born, streams = self.pending[key]
            if key[0] != self.active or S12 not in streams:
                if now - born >= self.timeout:
                    del self.pending[key]
                    self.stats['orphan'] += 1
                continue
            if not self.enabled.issubset(streams) and now - born < self.timeout:
                break  # keep frame order while waiting; UI never blocks
            del self.pending[key]
            if self.last_id is not None and key[1] <= self.last_id:
                continue
            stamp, count, _ = streams[S12]
            matched = {t: v[2] for t, v in streams.items() if v[:2] == (stamp, count)}
            self.stats['metadata_mismatch'] += len(streams) - len(matched)
            result.append((key[0], key[1], stamp, count, matched))
            self.last_id = key[1]
        return result
