"""Bounded rotating application logs, charged within auxiliary reservations."""
import os,pathlib,time
from .ownership import lock
_LAST={}
def event(c,level,message):
    rank={'INFO':0,'WARNING':1,'ERROR':2};cfg=c['logging']
    if rank[level]<rank[cfg['level']]:return
    key=(cfg['path'],level,message);now=time.monotonic()
    if now-_LAST.get(key,float('-inf'))<cfg['rate_limit_sec']:return
    _LAST.clear();_LAST[key]=now
    payload=(level+' '+message+'\n').encode()[:cfg['rotation_bytes']]
    p=pathlib.Path(cfg['path']);p.parent.mkdir(parents=True,exist_ok=True)
    with lock(str(p)+'.lock') as guard:
        if guard is None:return
        if p.exists() and p.stat().st_size+len(payload)>cfg['rotation_bytes']:
            for n in range(cfg['rotation_count'],0,-1):
                source=p if n==1 else pathlib.Path(str(p)+'.'+str(n-1));target=pathlib.Path(str(p)+'.'+str(n))
                if source.exists():os.replace(source,target)
            if cfg['rotation_count']==0:p.unlink(missing_ok=True)
        fd=os.open(p,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'ab') as f:f.write(payload)
