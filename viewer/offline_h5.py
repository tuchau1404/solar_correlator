"""Bounded, read-only solar storage schema-1 reader. No Qt or UDP dependency."""
from dataclasses import dataclass
from pathlib import Path
from collections import OrderedDict
import hashlib
import json
import numpy as np
import h5py

ARRAYS = ('s11_linear', 's22_linear', 's12_real', 's12_imag')
VECTORS = ('timestamps_us', 'start_utc_us', 'end_utc_us', 'sample_start',
           'sample_end', 'frame_ids_first', 'frame_ids_last', 'valid_fft_frames',
           'quality_flags')
MAX_RECORDS = 1_000_000
MAX_SELECTED = 200_000
# QDateTimeEdit default supported range, with room for the UTC+07 offset.
MIN_UTC_US = -14831769600000000  # 1500-01-01 UTC
MAX_UTC_US = 253402275600000000  # 9999-12-31 17:00 UTC


def text(value):
    return value.decode('utf8') if isinstance(value, bytes) else str(value)


def derived(a, b, re, im):
    mag = np.hypot(re, im)
    with np.errstate(invalid='ignore', divide='ignore', over='ignore'):
        gamma = mag / np.sqrt(a * b)
    valid = (np.isfinite(a) & np.isfinite(b) & np.isfinite(re) & np.isfinite(im)
             & (a > 0) & (b > 0) & (gamma <= 1 + 1e-5))
    # Tolerate float32 roundoff only; materially impossible coherence is hidden.
    return mag, np.where(valid, np.arctan2(im, re), np.nan), np.where(valid, np.minimum(gamma, 1), np.nan)


def db(x):
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(x > 0, 10 * np.log10(x), np.nan)


@dataclass
class Entry:
    path: str
    meta: dict
    vectors: dict
    signature: tuple
    stat: tuple


class Catalog:
    def __init__(self, paths, cancelled=lambda: False):
        self._blocks = OrderedDict()
        self._cache_bytes = 0
        self.cache_limit = 64 * 1024 * 1024
        self.entries = []
        self.records = []
        self.duplicates = 0
        seen = {}
        paths = list(dict.fromkeys(str(Path(p).resolve()) for p in paths))
        if not paths or len(paths) > 512:
            raise ValueError('Choose 1..512 closed H5 files.')
        freq = None
        for path in paths:
            if cancelled(): raise InterruptedError('Cancelled')
            stat = Path(path).stat()
            with h5py.File(path, 'r') as f:
                m = dict(f['metadata'].attrs)
                if int(m.get('data_schema_version', -1)) != 1:
                    raise ValueError(f'{Path(path).name}: unsupported data schema')
                for key in ('session_id', 'calibration_id', 'config_hash', 'units',
                            'cross_product_convention', 'sample_rate_hz', 'hop_samples'):
                    if key not in m: raise ValueError(f'Missing metadata: {key}')
                if text(m['units']) != 'ADC-count^2 per input FFT bin (mean after frequency grouping)':
                    raise ValueError('Unsupported power units')
                if text(m['cross_product_convention']) != 'X1*conj(X2) * exp(-j*phi)':
                    raise ValueError('Unsupported cross-product convention')
                fq = f['frequency_hz'][:]
                if fq.ndim != 1 or not 2 <= len(fq) <= 2048 or not np.all(np.isfinite(fq)) or not np.all(np.diff(fq) > 0):
                    raise ValueError('Invalid frequency grid')
                bandwidth = float(m['frequency_bin_bandwidth_hz'])
                if not np.isfinite(bandwidth) or bandwidth <= 0 or not np.allclose(np.diff(fq), bandwidth, rtol=1e-8, atol=1e-4):
                    raise ValueError('Unsupported nonuniform frequency grid')
                if freq is None: freq = fq; self.bandwidth = bandwidth
                elif not np.array_equal(freq, fq) or self.bandwidth != bandwidth:
                    raise ValueError('Frequency grids differ; open these files separately.')
                d = f['data']; n = d['timestamps_us'].shape[0]
                if n + len(self.records) > MAX_RECORDS:
                    raise ValueError('Catalog limit: 1,000,000 records. Choose fewer files.')
                if any(d[k].shape != (n, len(freq)) or d[k].dtype.kind != 'f' for k in ARRAYS):
                    raise ValueError('Invalid spectrum dimensions or types')
                if any(d[k].shape != (n,) or d[k].dtype.kind not in 'ui' for k in VECTORS):
                    raise ValueError('Invalid record vectors')
                v = {k: d[k][:] for k in VECTORS}
                if n and (np.any(v['start_utc_us'] >= v['end_utc_us']) or
                          np.any(v['timestamps_us'] < v['start_utc_us']) or
                          np.any(v['timestamps_us'] > v['end_utc_us']) or
                          np.any(v['sample_start'] >= v['sample_end']) or
                          np.any(v['valid_fft_frames'] <= 0) or
                          np.any(v['frame_ids_first'] > v['frame_ids_last']) or
                          np.any(v['start_utc_us'] < MIN_UTC_US) or
                          np.any(v['end_utc_us'] >= MAX_UTC_US) or
                          np.any(v['timestamps_us'][1:] <= v['timestamps_us'][:-1])):
                    raise ValueError('Invalid timestamps, sample intervals or weights')
                signature = tuple(text(m.get(k, '')) for k in (
                    'session_id', 'calibration_id', 'config_hash', 'phase_calibrated',
                    'cross_product_convention', 'normalization')) + (
                    hashlib.sha256(f['calibration_phi_rad_unshifted'][:].tobytes()).hexdigest(),)
                e = Entry(path, m, v, signature, (stat.st_size, stat.st_mtime_ns))
                ei = len(self.entries); self.entries.append(e)
                for i in range(n):
                    if i % 256 == 0 and cancelled(): raise InterruptedError('Cancelled')
                    key = (text(m['session_id']), int(v['sample_start'][i]), int(v['sample_end'][i]))
                    if key in seen:
                        pe, pi = seen[key]; old = self.entries[pe]
                        if signature != old.signature or any(v[k][i] != old.vectors[k][pi] for k in VECTORS):
                            raise ValueError('Conflicting duplicate record metadata')
                        with h5py.File(old.path, 'r') as previous:
                            if any(not np.array_equal(d[k][i], previous['data/'+k][pi], equal_nan=True) for k in ARRAYS):
                                raise ValueError('Conflicting duplicate spectrum data')
                        self.duplicates += 1
                        continue
                    seen[key] = (ei, i)
                    self.records.append((int(v['timestamps_us'][i]), ei, i))
        if not self.records: raise ValueError('No records in the selected files')
        self.freq = freq
        self.records.sort()
        self.times = np.array([r[0] / 1e6 for r in self.records])
        if np.any(np.diff(self.times) <= 0):
            raise ValueError('Overlapping timestamps across sessions; open sessions separately.')
        self.breaks = np.zeros(len(self.records), bool); self.breaks[0] = True
        for j in range(1, len(self.records)):
            _, ae, ai = self.records[j-1]; _, be, bi = self.records[j]
            a, b = self.entries[ae], self.entries[be]
            av, bv = a.vectors, b.vectors
            # Adjacent Welch integrations share one hop of sample support.
            hop = int(b.meta['hop_samples'])
            contiguous = int(bv['sample_start'][bi]) == int(av['sample_end'][ai]) - hop
            gap = int(bv['start_utc_us'][bi]) - int(av['end_utc_us'][ai])
            if a.signature == b.signature and not contiguous and gap < -1000:
                raise ValueError('Overlapping sample ranges; cannot merge safely')
            self.breaks[j] = (a.signature != b.signature or not contiguous or gap > 1000
                              or bool(int(bv['quality_flags'][bi]) & (8 | 16)))

    def read_spectrum(self, index, cancelled=lambda: False):
        """One saved H5 record, full frequency axis; no history reload."""
        if cancelled(): raise InterruptedError('Cancelled')
        stamp,ei,ri=self.records[index];e=self.entries[ei];stat=Path(e.path).stat()
        if (stat.st_size,stat.st_mtime_ns)!=e.stat:
            raise ValueError('An H5 file changed after indexing. Reopen only closed files.')
        with h5py.File(e.path,'r') as f:
            vals=np.array([f['data/'+k][ri,:] for k in ARRAYS],dtype=float)
        if cancelled(): raise InterruptedError('Cancelled')
        return dict(mean=vals,mean_abs=np.hypot(vals[2],vals[3]),timestamp=float(stamp),
                    path=e.path,row=ri)

    def read_block(self, ei, first):
        """Worker-only 64 MiB LRU; immutable 128-row blocks shared by all views."""
        e = self.entries[ei]; stat = Path(e.path).stat()
        if (stat.st_size, stat.st_mtime_ns) != e.stat:
            self._blocks.clear(); self._cache_bytes = 0
            raise ValueError('An H5 file changed after indexing. Reopen only closed files.')
        key = (ei, first)
        if key in self._blocks:
            self._blocks.move_to_end(key)
            return self._blocks[key]
        with h5py.File(e.path, 'r') as f:
            block = np.array([f['data/'+k][first:first+128] for k in ARRAYS], dtype=float)
        block.setflags(write=False)
        while self._blocks and self._cache_bytes + block.nbytes > self.cache_limit:
            _, old = self._blocks.popitem(last=False); self._cache_bytes -= old.nbytes
        if block.nbytes <= self.cache_limit:
            self._blocks[key] = block; self._cache_bytes += block.nbytes
        return block

    def select_traces(self, source, low, high, cancelled=lambda: False):
        """Read only requested columns; retain full-band display products."""
        if not np.isfinite(low+high) or low > high:
            raise ValueError('Invalid frequency selection')
        if low == high:
            if not self.freq[0]-self.bandwidth/2 <= low <= self.freq[-1]+self.bandwidth/2:
                raise ValueError('Selected frequency is outside the saved band')
            left = int(np.argmin(abs(self.freq-low))); right = left+1
        else:
            left = int(np.searchsorted(self.freq, low)); right = int(np.searchsorted(self.freq, high, side='right'))
        if left == right: raise ValueError('ROI contains no saved bin centers')
        chosen = np.searchsorted(self.times, source['times'])
        traces = np.full((4,len(chosen)), np.nan); power_traces=np.full((3,len(chosen)),np.nan); pos = 0
        while pos < len(chosen):
            if cancelled(): raise InterruptedError('Cancelled')
            _,ei,ri = self.records[chosen[pos]]; stop = pos+1
            while stop < min(len(chosen),pos+128):
                _,ne,nr = self.records[chosen[stop]]
                if ne != ei or nr != ri+stop-pos: break
                stop += 1
            e=self.entries[ei]; stat=Path(e.path).stat()
            if (stat.st_size,stat.st_mtime_ns)!=e.stat:
                raise ValueError('An H5 file changed after indexing. Reopen only closed files.')
            with h5py.File(e.path,'r') as f:
                vals=np.array([f['data/'+k][ri:ri+stop-pos,left:right] for k in ARRAYS],dtype=float)
            power_traces[:2,pos:stop]=vals[:2,:,0]
            power_traces[2,pos:stop]=np.hypot(vals[2,:,0],vals[3,:,0])
            valid=np.all(np.isfinite(derived(*vals)[2]),axis=1)
            traces[:,pos:stop]=np.where(valid[None,:],vals.mean(axis=2),np.nan)
            pos=stop
        result=dict(source);result['traces']=traces;result['frequency']=self.freq[left:right];result['power_traces']=power_traces
        return result

    def load(self, start, end, low, high, quantity='S12', cancelled=lambda: False, display_group=1, waterfall_only=False):
        chosen = np.flatnonzero((self.times >= start) & (self.times <= end))
        if not len(chosen): raise ValueError('No records in the selected time range')
        if len(chosen) > MAX_SELECTED: raise ValueError('Choose at most 200,000 records per view')
        if not np.isfinite(low + high) or low > high: raise ValueError('Invalid frequency selection')
        if low == high:
            if not self.freq[0] - self.bandwidth/2 <= low <= self.freq[-1] + self.bandwidth/2:
                raise ValueError('Selected frequency is outside the saved band')
            mask = np.zeros(len(self.freq), bool); mask[np.argmin(abs(self.freq-low))] = True
        else:
            mask = (self.freq >= low) & (self.freq <= high)
        if not mask.any(): raise ValueError('ROI contains no saved bin centers')
        # Only bounded blocks and <=1600 frequency rows reside in memory.
        group = max(1, int(display_group), (1 if waterfall_only else int(np.ceil(len(chosen)/1200))))
        traces = np.full((4, len(chosen)), np.nan)
        power_traces=np.full((3,len(chosen)),np.nan)
        weights = np.zeros(len(chosen)); flags = np.zeros(len(chosen), np.uint32)
        meshes = []; phase_meshes = []; total = np.zeros((4, len(self.freq))); count = np.zeros(len(self.freq))
        total_mag = np.zeros(len(self.freq))
        tiles = []; tile_sum = np.zeros_like(total); tile_w = np.zeros_like(count)
        tile_first = tile_last = None; tile_n = 0
        starts = np.empty(len(chosen)); ends = np.empty(len(chosen))
        breaks = self.breaks[chosen].copy(); breaks[0] = True

        def finish_tile():
            nonlocal tile_sum, tile_w, tile_n
            if not tile_n: return
            a = np.divide(tile_sum, tile_w, out=np.full_like(tile_sum, np.nan), where=tile_w > 0)
            mag, phase, gamma = derived(*a)
            values = {'S12': db(mag), 'S11': db(a[0]), 'S22': db(a[1]),
                      'Phase': phase, 'Coherence': gamma}[quantity]
            tiles.append((tile_first, tile_last, values, gamma, phase))
            tile_sum.fill(0); tile_w.fill(0); tile_n = 0

        def finish_segment():
            nonlocal tiles
            finish_tile()
            if tiles:
                edges = [x[0] for x in tiles] + [tiles[-1][1]]
                if np.any(np.diff(edges) <= 0): raise ValueError('Nonmonotonic waterfall edges')
                meshes.append((np.array(edges), np.array([x[2] for x in tiles]), np.array([x[3] for x in tiles])))
                phase_meshes.append((np.array(edges), np.array([x[4] for x in tiles]), np.array([x[3] for x in tiles])))
                tiles = []

        pos = 0
        while pos < len(chosen):
            if cancelled(): raise InterruptedError('Cancelled')
            _, ei, ri = self.records[chosen[pos]]
            stop = pos + 1
            while stop < min(len(chosen), pos+128-ri%128):
                _, ne, nr = self.records[chosen[stop]]
                if ne != ei or nr != ri + stop-pos: break
                stop += 1
            e = self.entries[ei]; stat = Path(e.path).stat()
            if (stat.st_size, stat.st_mtime_ns) != e.stat:
                raise ValueError('An H5 file changed after indexing. Reopen only closed files.')
            first=(ri//128)*128
            block=self.read_block(ei,first)[:,ri-first:ri-first+stop-pos].copy()
            power_bin=np.flatnonzero(mask)[0]
            if not waterfall_only:
                power_traces[:2,pos:stop]=block[:2,:,power_bin]
                power_traces[2,pos:stop]=np.hypot(block[2,:,power_bin],block[3,:,power_bin])
            block_valid=np.isfinite(derived(*block)[2])
            block=np.where(block_valid[None,:,:],block,np.nan)
            block_weights=e.vectors['valid_fft_frames'][ri:ri+stop-pos].astype(float)
            clean=np.nan_to_num(block,nan=0)
            if not waterfall_only:
                total += (clean*block_weights[None,:,None]).sum(axis=1)
                count += (block_valid*block_weights[:,None]).sum(axis=0)
                total_mag += (np.nan_to_num(np.hypot(block[2],block[3]),nan=0)*block_weights[:,None]).sum(axis=0)
                selected_valid=np.all(block_valid[:,mask],axis=1)
                traces[:,pos:stop]=np.where(selected_valid[None,:],block[:,:,mask].mean(axis=2),np.nan)
            for off in range(stop-pos):
                if cancelled():raise InterruptedError("Cancelled")
                j = pos+off; row = ri+off
                if breaks[j]:
                    finish_segment()
                    if len(meshes) >= 200: raise ValueError('Too many discontinuities; choose a shorter range')
                vals = block[:, off]
                valid = block_valid[off]
                v = e.vectors; w = float(v['valid_fft_frames'][row]); weights[j] = w
                flags[j] = v['quality_flags'][row]
                starts[j] = int(v['start_utc_us'][row])/1e6
                ends[j] = int(v['end_utc_us'][row])/1e6
                if tile_n == 0: tile_first = starts[j]
                tile_last = ends[j]; tile_n += 1
                tile_sum += clean[:,off]*w; tile_w += valid*w
                if tile_n >= group: finish_tile()
            pos = stop
        finish_segment()
        mean = np.divide(total, count, out=np.full_like(total, np.nan), where=count > 0)
        mean_abs = np.divide(total_mag, count, out=np.full_like(total_mag, np.nan), where=count > 0)
        return dict(times=self.times[chosen], starts=starts, ends=ends, traces=traces, power_traces=power_traces,
                    flags=flags, weights=weights, breaks=breaks, meshes=meshes, mean=mean,
                    group=group, frequency=self.freq[mask], quantity=quantity, mean_abs=mean_abs,
                    phase_meshes=phase_meshes)


def broken_series(times, values, breaks):
    """Insert NaNs before boundaries without discarding either adjacent record."""
    positions = np.flatnonzero(breaks)[1:]
    return np.insert(times, positions, np.nan), np.insert(values, positions, np.nan)
