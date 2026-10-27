"""Disposable, atomic, non-pickle cache of complete H5 display results.

Size/mtime fingerprints detect ordinary edits, not malicious same-stat edits.
Concurrent writers publish independent complete files with os.replace.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile
import numpy as np

VERSION = 1
BUDGET = 512 * 1024**2
FIELDS = ('times','starts','ends','traces','power_traces','flags','weights',
          'breaks','mean','frequency','mean_abs')


def root():
    if os.environ.get('SOLAR_H5_CACHE') == 'off':return None
    override=os.environ.get('SOLAR_H5_CACHE')
    base=Path(os.environ.get('LOCALAPPDATA') or os.environ.get('XDG_CACHE_HOME') or Path.home()/'.cache')
    return Path(override) if override else base/'solar_correlator'/'h5-v1'


def fingerprint(catalog, args):
    files=[]
    for e in catalog.entries:
        stat=Path(e.path).stat()
        if (stat.st_size,stat.st_mtime_ns)!=e.stat:
            raise ValueError('An H5 file changed. Reopen only closed files.')
        files.append((str(Path(e.path).resolve()),*e.stat))
    payload=json.dumps((VERSION,files,args),sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def read(key):
    directory=root()
    if directory is None:return None
    path=directory/(key+'.npz')
    try:
        if path.stat().st_size>BUDGET:return None
        with np.load(path,allow_pickle=False) as z:
            # Bound uncompressed allocations even if the cache was corrupted.
            with zipfile.ZipFile(path) as archive:
                if sum(i.file_size for i in archive.infolist())>BUDGET:return None
            result={name:z[name] for name in FIELDS}
            result['group']=int(z['group']);result['quantity']=str(z['quantity'])
            for name in ('meshes','phase_meshes'):
                n=int(z[name+'_count'])
                if not 0<=n<=1000:return None
                result[name]=[tuple(z[f'{name}_{i}_{j}'] for j in range(3)) for i in range(n)]
        n=len(result['times']);bins=result['mean'].shape[1]
        if result['mean'].shape!=(4,bins) or result['traces'].shape!=(4,n) or result['power_traces'].shape!=(3,n) or result['group']<1:
            return None
        for name in ('meshes','phase_meshes'):
            for edges,values,gamma in result[name]:
                if values.shape!=(len(edges)-1,bins) or gamma.shape!=values.shape or not np.all(np.diff(edges)>0):return None
        os.utime(path,None)
        return result
    except (OSError,ValueError,KeyError,EOFError,OverflowError,IndexError,zipfile.BadZipFile):return None


def write(key,result,cancelled):
    directory=root()
    if directory is None:return
    values={name:result[name] for name in FIELDS}
    values.update(group=result['group'],quantity=result['quantity'])
    for name in ('meshes','phase_meshes'):
        values[name+'_count']=len(result[name])
        for i,mesh in enumerate(result[name]):
            for j,a in enumerate(mesh):values[f'{name}_{i}_{j}']=a
    if sum(np.asarray(a).nbytes for a in values.values())>BUDGET:return
    temporary=None
    try:
        directory.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory,suffix='.tmp',delete=False) as f:
            temporary=Path(f.name);np.savez(f,**values)
        if cancelled():return
        os.replace(temporary,directory/(key+'.npz'));temporary=None
        files=sorted(directory.glob('*.npz'),key=lambda p:p.stat().st_mtime_ns)
        total=sum(p.stat().st_size for p in files)
        for p in files:
            if total<=BUDGET:break
            size=p.stat().st_size;p.unlink();total-=size
        for p in directory.glob('*.tmp'):
            import time
            if time.time()-p.stat().st_mtime>86400:p.unlink(missing_ok=True)
    except (OSError,ValueError):pass
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
