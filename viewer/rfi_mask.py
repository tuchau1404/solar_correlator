"""Qt-free, per-observation narrow-line candidates and weighted reductions.

bad_mask=True means excluded. Missing/invalid statistics are a separate state.
No interpolation, harmonic grid, phase reconstruction, or temporal persistence.
"""
from dataclasses import dataclass, asdict
import warnings
import numpy as np


@dataclass(frozen=True)
class RfiConfig:
    enabled: bool = False
    mode: str = 'Combined'
    overlay: bool = False
    ranges_mhz: tuple = ()
    window_hz: float = 250_000.
    sigma: float = 6.
    prominence_db: float = 6.
    max_width_hz: float = 50_000.
    expand_hz: float = 5_000.
    min_valid_fraction: float = .5

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.overlay) is not bool:
            raise ValueError('Boolean switches required')
        if self.mode not in ('Manual', 'Auto', 'Combined'):
            raise ValueError('Invalid RFI mode')
        bounds = {'window_hz': (1., 1e8), 'sigma': (1., 100.),
                  'prominence_db': (.1, 100.), 'max_width_hz': (1., 1e8),
                  'expand_hz': (0., 1e7), 'min_valid_fraction': (0., 1.)}
        for key, (lo, hi) in bounds.items():
            x = getattr(self, key)
            if type(x) not in (int, float) or not np.isfinite(x) or not lo <= x <= hi:
                raise ValueError('Invalid '+key)
        if self.max_width_hz >= self.window_hz / 2:
            raise ValueError('Max line width must be less than half the background window')
        if len(self.ranges_mhz) > 256:
            raise ValueError('At most 256 manual intervals')
        clean = []
        for interval in self.ranges_mhz:
            if len(interval) != 2:
                raise ValueError('A manual interval needs two limits')
            lo, hi = interval
            if (type(lo) not in (int, float) or type(hi) not in (int, float)
                    or not np.isfinite(lo+hi) or not 0 <= lo <= hi <= 100000):
                raise ValueError('Manual MHz limits must be finite, ordered and nonnegative')
            clean.append((float(lo), float(hi)))
        object.__setattr__(self, 'ranges_mhz', tuple(clean))

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError('Invalid RFI configuration')
        return cls(**value)


def frequency_edges(frequency_hz):
    f = np.asarray(frequency_hz, dtype=float)
    if f.ndim != 1 or len(f) < 2 or not np.isfinite(f).all() or not np.all(np.diff(f) > 0):
        raise ValueError('Need at least two finite increasing frequency centers')
    return np.r_[f[0]-(f[1]-f[0])/2, (f[:-1]+f[1:])/2, f[-1]+(f[-1]-f[-2])/2]


def power_db(power):
    p = np.asarray(power, dtype=float)
    out = np.full(p.shape, np.nan)
    good = np.isfinite(p) & (p > 0)
    out[good] = 10*np.log10(p[good])
    return out


def local_median(values, f, width):
    # Fast uniform-grid case. Window is set in Hz, never a fixed source bin count.
    step = np.diff(f)
    if np.allclose(step, step[0], rtol=1e-7):
        radius = min(len(f)-1, int(np.floor(width / (2*step[0]))))
        padded = np.pad(values, radius, constant_values=np.nan)
        view = np.lib.stride_tricks.sliding_window_view(padded, 2*radius+1)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            return np.nanmedian(view, axis=-1)
    result = np.full(len(f), np.nan)
    for i, x in enumerate(f):
        part = values[np.searchsorted(f,x-width/2):np.searchsorted(f,x+width/2,side='right')]
        part = part[np.isfinite(part)]
        if part.size: result[i] = np.median(part)
    return result


def narrow_candidates(db, frequency_hz, config):
    """Median background in dB; local 1.4826*MAD, with a 0.1 dB floor.

    Seed > max(prominence, sigma*scale); support > half that threshold.
    Reject support components wider than max_width_hz, then dilate their
    measured cell-edge support by expand_hz. Independent rows track drift with
    zero persistence delay. Broad events with embedded narrow lines can still
    be flagged: this is a candidate detector, not an astronomical classifier.
    """
    f = np.asarray(frequency_hz, float); edges = frequency_edges(f)
    y = np.asarray(db, float)
    if y.shape != f.shape: raise ValueError('Power and frequency shapes differ')
    finite = np.isfinite(y)
    y = np.where(finite, y, np.nan)
    baseline = local_median(y, f, config.window_hz)
    residual = y-baseline
    center = local_median(residual, f, config.window_hz)
    scale = 1.4826*local_median(np.abs(residual-center), f, config.window_hz)
    threshold = np.maximum(config.prominence_db, config.sigma*np.maximum(scale, .1))
    support = finite & (residual > threshold*.5)
    seeds = finite & (residual > threshold)
    result = np.zeros(len(f), bool)
    changes = np.diff(np.r_[False,support,False].astype(np.int8))
    for a,b in zip(np.flatnonzero(changes==1), np.flatnonzero(changes==-1)):
        if seeds[a:b].any() and edges[b]-edges[a] <= config.max_width_hz:
            result |= (edges[1:] > edges[a]-config.expand_hz) & (edges[:-1] < edges[b]+config.expand_hz)
    return result & finite


@dataclass
class MaskResult:
    bad_mask: np.ndarray
    missing: np.ndarray
    manual: np.ndarray
    auto: np.ndarray
    detector_channels: int


def make_mask(frequency_hz, config, s11=None, s22=None, *, domain='linear', available=None):
    f = np.asarray(frequency_hz, float); frequency_edges(f)
    powers = [np.asarray(p, float) for p in (s11,s22) if p is not None]
    if domain not in ('linear','db'): raise ValueError('domain must be linear or db')
    if any(p.shape != f.shape for p in powers): raise ValueError('Spectrum shape mismatch')
    ys = [power_db(p) if domain=='linear' else np.where(np.isfinite(p),p,np.nan) for p in powers]
    if available is None:
        available = np.logical_or.reduce([np.isfinite(y) for y in ys]) if ys else np.zeros(f.shape,bool)
    available = np.asarray(available,bool)
    if available.shape != f.shape: raise ValueError('Availability shape mismatch')
    manual = np.zeros(f.shape,bool)
    for lo,hi in config.ranges_mhz: manual |= (f >= lo*1e6) & (f <= hi*1e6)
    auto = np.zeros(f.shape,bool)
    if config.mode != 'Manual':
        for y in ys: auto |= narrow_candidates(y,f,config)
    manual &= available; auto &= available
    bad = manual if config.mode=='Manual' else auto if config.mode=='Auto' else manual|auto
    return MaskResult(bad, ~available, manual, auto, len(ys))


def weighted_mean(values, weights, valid, axis=0):
    """Select before arithmetic: invalid entries never contribute 0*NaN/Inf."""
    a = np.asarray(values)
    w = np.broadcast_to(np.asarray(weights,float), a.shape)
    good = np.broadcast_to(np.asarray(valid,bool), a.shape) & np.isfinite(a) & np.isfinite(w) & (w>0)
    safe = np.zeros(a.shape, dtype=np.result_type(a.dtype,np.float64))
    np.multiply(a,w,out=safe,where=good)
    denom = np.sum(np.where(good,w,0.),axis=axis)
    return np.divide(safe.sum(axis=axis),denom,
                     out=np.full(np.shape(denom),np.nan,dtype=safe.dtype),where=denom>0),denom


def common_statistics(s11,s22,s12,weights,bad_mask,axis=0):
    """One shared support for the three sufficient statistics; no coherence mean."""
    a,b,c = np.broadcast_arrays(s11,s22,s12)
    valid = np.isfinite(a)&np.isfinite(b)&np.isfinite(c)&(a>0)&(b>0)&~np.asarray(bad_mask,bool)
    aa,w = weighted_mean(a,weights,valid,axis)
    bb,_ = weighted_mean(b,weights,valid,axis)
    cc,_ = weighted_mean(c,weights,valid,axis)
    with np.errstate(divide='ignore',invalid='ignore',over='ignore'):
        coherence = np.abs(cc)/np.sqrt(aa*bb)
    # Hide materially impossible results; tolerate roundoff only.
    coherence = np.where(coherence <= 1+1e-5, np.minimum(coherence,1.),np.nan)
    return aa,bb,cc,coherence,w


class VersionGate:
    """Worker results require matching configuration AND data/source generation."""
    def __init__(self): self.version = 0
    def invalidate(self): self.version += 1; return self.version
    def accepts(self, version): return version == self.version


class MaskCache:
    """Bounded, single-worker cache for one immutable catalog/configuration.

    Packed masks only; file identity/stat validation belongs to Catalog.
    Every configuration change clears prior entries, including cancelled work.
    """
    def __init__(self, max_bytes=8*1024*1024):
        from collections import OrderedDict
        self.entries=OrderedDict();self.config=None
        self.max_bytes=max_bytes;self.bytes=0

    def configure(self,config):
        if config != self.config:
            self.entries.clear();self.bytes=0;self.config=config

    def get(self,config,key):
        self.configure(config)
        item=self.entries.get(key)
        if item is None:return None
        self.entries.move_to_end(key)
        packed,n=item
        return np.unpackbits(packed,count=n).astype(bool)

    def put(self,config,key,mask):
        self.configure(config)
        packed=np.packbits(mask)
        if packed.nbytes>self.max_bytes:return
        old=self.entries.pop(key,None)
        if old is not None:self.bytes-=old[0].nbytes
        while self.entries and self.bytes+packed.nbytes>self.max_bytes:
            _,old=self.entries.popitem(last=False);self.bytes-=old[0].nbytes
        self.entries[key]=(packed,len(mask));self.bytes+=packed.nbytes
