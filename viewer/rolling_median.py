"""Qt-free rolling median of per-record dB magnitudes; no RFI classification.

Residuals are signed. Clipping is a display operation after reduction.
Window support uses actual UTC, never an assumed frame cadence.
"""
from dataclasses import dataclass, asdict
from collections import OrderedDict
from pathlib import Path
import warnings
import numpy as np

MAX_WINDOW_CELLS = 8_388_608


@dataclass(frozen=True)
class RollingConfig:
    enabled: bool = False
    window_s: float = 60.
    alignment: str = 'Trailing'
    clip: bool = True
    min_samples: int = 3

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.clip) is not bool:
            raise ValueError('Boolean switches required')
        if type(self.window_s) not in (int,float) or not np.isfinite(self.window_s) or not .1 <= self.window_s <= 600:
            raise ValueError('Window must be 0.1–600 seconds')
        if self.alignment not in ('Trailing','Centered'):
            raise ValueError('Alignment must be Trailing or Centered')
        if type(self.min_samples) is not int or not 1 <= self.min_samples <= 10000:
            raise ValueError('Minimum samples must be 1–10000')

    def to_dict(self):return asdict(self)

    @classmethod
    def from_dict(cls,value):
        if not isinstance(value,dict) or set(value)-set(cls.__dataclass_fields__):
            raise ValueError('Invalid rolling configuration')
        return cls(**value)


def window_bounds(times, breaks, config):
    t=np.asarray(times,float);br=np.asarray(breaks,bool)
    if t.ndim!=1 or br.shape!=t.shape or not np.isfinite(t).all():
        raise ValueError('Invalid rolling timestamps/breaks')
    if not len(t):return []
    if not br[0]:raise ValueError('First record must start a segment')
    result=[None]*len(t)
    starts=np.flatnonzero(br)
    for a,b in zip(starts,np.r_[starts[1:],len(t)]):
        seg=t[a:b]
        if np.any(np.diff(seg)<=0):raise ValueError('Time must increase within a segment')
        before=config.window_s if config.alignment=='Trailing' else config.window_s/2
        after=0 if config.alignment=='Trailing' else config.window_s/2
        for j,x in enumerate(seg,a):
            lo=a+int(np.searchsorted(seg,x-before,side='left'))
            hi=a+int(np.searchsorted(seg,x+after,side='right'))
            result[j]=(lo,hi)
    return result


def residual_at(value, window, config):
    arr=np.asarray(window)
    if arr.ndim!=2 or arr.shape[1:]!=np.asarray(value).shape:
        raise ValueError('Invalid rolling window shape')
    if arr.size>MAX_WINDOW_CELLS:
        raise ValueError('Rolling window too large; reduce window seconds')
    finite=np.isfinite(arr);arr=np.where(finite,arr,np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore',RuntimeWarning)
        median=np.nanmedian(arr,axis=0)
    good=(finite.sum(axis=0)>=config.min_samples)&np.isfinite(value)&np.isfinite(median)
    result=np.full(np.asarray(value).shape,np.nan,dtype=np.float32)
    np.subtract(value,median,out=result,where=good,casting='unsafe')
    return result


class LogMean:
    """FFT-weighted linear-ratio mean accumulated in a stable logarithmic form."""
    def __init__(self,bins):
        self.peak=np.full(bins,-np.inf);self.total=np.zeros(bins);self.weight=np.zeros(bins)

    def add(self,db,weight):
        if not np.isfinite(weight) or weight<=0:raise ValueError('Invalid FFT weight')
        value=np.asarray(db,float);good=np.isfinite(value)
        new=np.maximum(self.peak[good],value[good])
        self.total[good]=(self.total[good]*np.power(10.,(self.peak[good]-new)/10.)
                          +weight*np.power(10.,(value[good]-new)/10.))
        self.peak[good]=new;self.weight[good]+=weight

    def mean(self):
        result=np.full(len(self.peak),np.nan);good=self.weight>0
        result[good]=self.peak[good]+10*np.log10(self.total[good]/self.weight[good])
        return result.astype(np.float32)


def row_key(row):return tuple(row[:4])
def group_key(group):return tuple(group[:6])


def live_residuals(rows,groups,config,previous=None,cancelled=lambda:False,previous_deep=None,progress=lambda done,total:None):
    """Incremental cache over immutable live rows; input arrays remain untouched.

    A signature changes at either window boundary or a segment boundary.
    Centered edge results are provisional and update when future frames arrive.
    """
    from telemetry_protocol import S12
    from science_model import PERIOD
    present=[];breaks=[];last=None;gap=True
    for row in rows:
        if row is None:gap=True;continue
        br=(gap or last is None or row[0]!=last[0] or row[1]!=last[1]+1
            or row[2]<=last[2] or row[2]-last[2]>PERIOD*1.5e6)
        present.append(row);breaks.append(br);last=row;gap=False
    times=np.array([r[2]/1e6 for r in present]);bounds=window_bounds(times,breaks,config)
    previous=previous or {};previous_deep=previous_deep or {};cache={};residuals={}
    for j,(lo,hi) in enumerate(bounds):
        if cancelled():raise InterruptedError('Cancelled')
        progress(j,len(bounds));row=present[j];key=row_key(row)
        signature=(config.window_s,config.alignment,config.min_samples,row_key(present[lo]),row_key(present[hi-1]),hi-lo)
        old=previous.get(key)
        if old is not None and old[0]==signature:result=old[1]
        else:
            bins=len(row[4][S12])
            if (hi-lo)*bins>MAX_WINDOW_CELLS:raise ValueError('Live median window too large; reduce seconds')
            window=np.array([r[4][S12] for r in present[lo:hi]],dtype=np.float32)
            result=residual_at(row[4][S12],window,config)
        cache[key]=(signature,result);residuals[key]=result
    by_id={(r[0],r[1]):r for r in present};deep={}
    for group in groups:
        if cancelled():raise InterruptedError('Cancelled')
        members=[by_id.get((group[0],fid)) for fid in range(group[2],group[3]+1)]
        if not members or any(r is None for r in members):continue
        key=group_key(group)
        if key in previous_deep and all(row_key(r) in previous and cache[row_key(r)][1] is previous[row_key(r)][1] for r in members):
            deep[key]=previous_deep[key];continue
        mean=LogMean(len(members[0][4][S12]))
        for row in members:mean.add(residuals[row_key(row)],row[3])
        deep[group_key(group)]=mean.mean()
    progress(len(bounds),len(bounds))
    return cache,deep


class H5Rows:
    """Worker-only small LRU of converted input rows, not a full-history copy."""
    def __init__(self,catalog,chosen,quantity,cancelled):
        self.catalog,self.chosen,self.quantity,self.cancelled=catalog,chosen,quantity,cancelled
        self.blocks=OrderedDict()

    def __call__(self,index):
        import h5py
        from offline_h5 import ARRAYS,derived,db
        if self.cancelled():raise InterruptedError('Cancelled')
        _,ei,ri=self.catalog.records[self.chosen[index]]
        first=(ri//128)*128;key=(ei,first)
        if key not in self.blocks:
            e=self.catalog.entries[ei];stat=Path(e.path).stat()
            if (stat.st_size,stat.st_mtime_ns)!=e.stat:raise ValueError('H5 changed; reopen a closed file')
            vals=self.catalog.read_block(ei,first)
            mag,_,gamma=derived(*vals)
            value={'S12':mag,'S11':vals[0],'S22':vals[1]}[self.quantity]
            with np.errstate(divide='ignore',invalid='ignore'):
                value_db=10*np.log10(value+1e-12)
            self.blocks[key]=np.where(np.isfinite(gamma),value_db,np.nan).astype(np.float32)
            while len(self.blocks)>8:self.blocks.popitem(last=False)
        self.blocks.move_to_end(key)
        return self.blocks[key][ri-first]


def h5_residuals(catalog,fast,deep,config,cancelled=lambda:False,progress=lambda done,total:None):
    """Median at saved input resolution, before Fast/Deep display grouping.

    Window context is limited to selected time range and split at catalog gaps.
    Raw spectra/traces/phase_meshes remain the original sufficient statistics.
    """
    chosen=np.searchsorted(catalog.times,fast['times'])
    bounds=window_bounds(fast['times'],fast['breaks'],config)
    reader=H5Rows(catalog,chosen,fast['quantity'],cancelled)
    outputs=[]
    for source in (fast,deep):
        result=dict(source)
        result['rolling_config']=config
        result['meshes']=[(edges,np.full(values.shape,np.nan,np.float32),gamma)
                          for edges,values,gamma in source['meshes']]
        outputs.append(result)
    # Accumulate only the current tile for each view; preserve existing segments.
    current=[None,None];acc=[None,None];segment=0
    def flush(which):
        if current[which] is not None:
            si,ti=current[which];values=acc[which].mean()
            if config.clip:values=np.maximum(values,0)
            outputs[which]['meshes'][si][1][ti]=values
    for j,(lo,hi) in enumerate(bounds):
        if cancelled():raise InterruptedError('Cancelled')
        if (hi-lo)*len(catalog.freq)>MAX_WINDOW_CELLS:raise ValueError('H5 median window too large; reduce seconds')
        progress(j,len(bounds))
        window=np.array([reader(k) for k in range(lo,hi)],dtype=np.float32)
        residual=residual_at(reader(j),window,config)
        if j and fast['breaks'][j]:segment+=1
        for which in (0,1):
            edges=outputs[which]['meshes'][segment][0]
            tile=int(np.searchsorted(edges,fast['times'][j],side='right')-1)
            if not 0<=tile<len(edges)-1:raise ValueError('Record is outside display tile support')
            target=(segment,tile)
            if target!=current[which]:
                flush(which);current[which]=target;acc[which]=LogMean(len(catalog.freq))
            acc[which].add(residual,fast['weights'][j])
    for which in (0,1):flush(which)
    progress(len(bounds),len(bounds))
    return tuple(outputs)
