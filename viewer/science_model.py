"""Bounded observation history. No smoothing or averaging of phase."""
from collections import deque
import numpy as np
from telemetry_protocol import BINS, S12, PHASE, COHERENCE

START, STOP = 30., 40.
STEP = (STOP - START) / BINS
PERIOD = 1953 * 1024 / 10e6


def masked_phase(streams, threshold):
    phase, gamma = streams.get(PHASE), streams.get(COHERENCE)
    result = np.full(BINS, np.nan, dtype=np.float32)
    if phase is not None and gamma is not None:
        good = (np.isfinite(phase) & np.isfinite(gamma) & (gamma > 0)
                & (gamma >= threshold) & (gamma <= 1) & (np.abs(phase) <= np.pi + 1e-6))
        result[good] = np.rad2deg(phase[good])
    return result


class ObservationHistory:
    def __init__(self, history=300, frequency=35.5):
        self.capacity = history
        self.rows = deque(maxlen=history)
        self.trace = deque(maxlen=history)
        self.session = None
        self.last_id = None
        self.select(frequency)

    def select(self, frequency):
        self.requested = float(frequency)
        self.bin = int(np.clip(np.floor((self.requested - START) / STEP + .5), 0, BINS-1))
        self.actual = START + self.bin * STEP
        self.trace.clear()

    def push(self, frame):
        session, fid, stamp, count, streams = frame
        if session != self.session:
            self.rows.clear(); self.trace.clear()
            self.session, self.last_id = session, None
        if self.last_id is not None:
            if fid <= self.last_id:
                return
            for _ in range(min(self.capacity, fid - self.last_id - 1)):
                self.rows.append(None); self.trace.append(None)
        self.rows.append(frame); self.trace.append(frame)
        self.last_id = fid

    def image(self, kind, threshold=.5):
        result = np.full((self.capacity, BINS), np.nan, dtype=np.float32)
        for i, row in enumerate(self.rows, self.capacity-len(self.rows)):
            if row is not None:
                result[i] = masked_phase(row[4], threshold) if kind == PHASE else row[4].get(kind, np.nan)
        return result

    def time_series(self, threshold):
        x, y, coherence = [], [], []
        previous = None
        for row in self.trace:
            if row is None:
                previous = None
                x.append(np.nan); y.append(np.nan); coherence.append(np.nan)
                continue
            session, fid, stamp, count, streams = row
            value = float(masked_phase(streams, threshold)[self.bin])
            gamma = float(streams[COHERENCE][self.bin]) if COHERENCE in streams else np.nan
            if previous is not None:
                ps, pf, pt, pv = previous
                if (session != ps or fid != pf + 1 or stamp <= pt
                        or stamp - pt > PERIOD * 1.5e6 or abs(value - pv) > 180):
                    x.append(np.nan); y.append(np.nan); coherence.append(np.nan)
            x.append(stamp / 1e6); y.append(value); coherence.append(gamma)
            previous = (session, fid, stamp, value) if np.isfinite(value) else None
            if not np.isfinite(value):
                # Leave a separator even if gamma exists but phase is absent/masked.
                x.append(np.nan); y.append(np.nan); coherence.append(np.nan)
        return np.asarray(x), np.asarray(y), np.asarray(coherence)
