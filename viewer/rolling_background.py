"""Exact per-bin incremental medians and independent signed-background cache."""
import os
import tempfile
import zipfile
import numpy as np
from h5_cache import root, fingerprint, BUDGET
try:
    from numba import njit
except ImportError:
    njit=None


def _kernel(values, lows, highs):
    bins=values.shape[1]
    width=int(np.max(highs-lows))
    ordered=np.empty((bins,width),np.float32)
    counts=np.zeros(bins,np.int64)
    output=np.full((len(lows),bins),np.nan,np.float32)
    valid=np.zeros((len(lows),bins),np.int32)
    oldlo=int(lows[0]);oldhi=int(highs[0])
    for b in range(bins):
        n=0
        for k in range(oldlo,oldhi):
            value=values[k,b]
            if np.isfinite(value):ordered[b,n]=value;n+=1
        ordered[b,:n]=np.sort(ordered[b,:n]);counts[b]=n
    for row in range(len(lows)):
        lo=int(lows[row]);hi=int(highs[row])
        # Segment jumps discard the previous window completely.
        remove_end=min(lo,oldhi)
        for k in range(oldlo,remove_end):
            for b in range(bins):
                value=values[k,b]
                if not np.isfinite(value):continue
                n=counts[b];left=0;right=n
                while left<right:
                    mid=(left+right)//2
                    if ordered[b,mid]<value:left=mid+1
                    else:right=mid
                for j in range(left,n-1):ordered[b,j]=ordered[b,j+1]
                counts[b]=n-1
        for k in range(max(oldhi,lo),hi):
            for b in range(bins):
                value=values[k,b]
                if not np.isfinite(value):continue
                n=counts[b];left=0;right=n
                while left<right:
                    mid=(left+right)//2
                    if ordered[b,mid]<value:left=mid+1
                    else:right=mid
                for j in range(n,left,-1):ordered[b,j]=ordered[b,j-1]
                ordered[b,left]=value;counts[b]=n+1
        for b in range(bins):
            n=counts[b];valid[row,b]=n
            if n:
                output[row,b]=ordered[b,n//2] if n%2 else (ordered[b,n//2-1]+ordered[b,n//2])/2
        oldlo=lo;oldhi=hi
    return output,valid


OUTPUT_CHUNK = 512

incremental = njit(cache=True, nogil=True)(_kernel) if njit else None


def background(reader,bounds,first,stop,key,cancelled):
    """One bounded output chunk, reusable across bin, clip and display grouping."""
    directory=root();path=None if directory is None else directory/(key+'.background.npz')
    shape=(stop-first,len(reader(first)))
    if path is not None:
        try:
            with zipfile.ZipFile(path) as archive:
                if sum(i.file_size for i in archive.infolist())>BUDGET:raise ValueError('Oversized cache')
            with np.load(path,allow_pickle=False) as f:
                median,count=f['median'],f['count']
            if median.shape==shape and count.shape==shape:
                os.utime(path,None);return median,count
        except (OSError,ValueError,KeyError,EOFError,zipfile.BadZipFile):pass
    if cancelled():raise InterruptedError('Cancelled')
    selected=np.asarray(bounds[first:stop],dtype=np.int64)
    from rolling_median import MAX_WINDOW_CELLS
    if int(np.max(selected[:,1]-selected[:,0]))*shape[1]>MAX_WINDOW_CELLS:
        raise ValueError('H5 median window too large; reduce seconds')
    lo=int(selected[:,0].min());hi=int(selected[:,1].max())
    # Retain the overlapping window across output chunks, with a bounded
    # converted-row budget; the old eight-block LRU thrashed at 600 s.
    if hasattr(reader, 'reserve'):
        reader.reserve(hi-lo)
    values=np.empty((hi-lo,shape[1]),dtype=np.float32)
    for offset,k in enumerate(range(lo,hi)):
        if cancelled():raise InterruptedError('Cancelled')
        values[offset]=reader(k)
    selected=selected-lo
    if incremental is not None:median,count=incremental(values,selected[:,0],selected[:,1])
    else:
        import warnings
        median=np.full(shape,np.nan,np.float32);count=np.zeros(shape,np.int32)
        for i,(a,b) in enumerate(selected):
            if cancelled():raise InterruptedError('Cancelled')
            window=values[a:b];count[i]=np.isfinite(window).sum(axis=0)
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',RuntimeWarning);median[i]=np.nanmedian(window,axis=0)
    if cancelled():raise InterruptedError('Cancelled')
    temporary=None
    if path is not None:
        try:
            directory.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory,suffix='.tmp',delete=False) as f:
                temporary=f.name;np.savez(f,median=median,count=count)
            os.replace(temporary,path);temporary=None
            files=sorted(directory.glob('*.npz'),key=lambda p:p.stat().st_mtime_ns)
            total=sum(p.stat().st_size for p in files)
            for p in files:
                if total<=BUDGET:break
                size=p.stat().st_size;p.unlink();total-=size
        except OSError:pass
        finally:
            if temporary is not None:
                from pathlib import Path
                Path(temporary).unlink(missing_ok=True)
    return median,count
