#!/usr/bin/env python3
import argparse,hashlib,json,os,pathlib,sys
from solar_storage.config import load
from solar_storage.ledger import Ledger,utc_us,digest
from solar_storage.ownership import lock,artifact_lock,owned_path
from solar_storage.reader import open_final,snapshot
from solar_storage.panels import render

def _generate(c,aid):
    ledger=Ledger(c)
    try:
        row=ledger.db.execute('SELECT * FROM artifact WHERE id=?',(aid,)).fetchone()
        if not row or row['state']!='READY' or row['kind']!='h5':raise ValueError('HDF5 not ready')
        a=dict(row)
        child=ledger.db.execute("SELECT * FROM artifact WHERE parent_id=? AND kind='png'",(aid,)).fetchone()
        if child and child['state']=='READY':
            existing=owned_path(c,child['path'])
            if existing.stat().st_size!=child['size'] or digest(existing)!=child['digest']:raise ValueError('immutable PNG changed')
            return
        if c['retention']['enabled'] and utc_us()-a['start_utc_us']>=c['retention']['days']*86400e6:return
        with lock(artifact_lock(c,aid),exclusive=False) as guard:
            if guard is None:return False
            path=owned_path(c,a['path'])
            with open_final(path) as f:
                cfg=snapshot(f)
                if not cfg['quicklook']['enabled']:return
                payload,summary=render(f)
            png_id=aid+'_a';png_path=str(path.with_suffix('.png'))
            with lock(artifact_lock(c,png_id)) as png_guard:
                if png_guard is None:return False
                artifact=ledger.prepare_png(aid,png_id,png_path)
                if artifact['state']=='READY':return
                if artifact['state']!='WRITING':raise ValueError('PNG terminal lifecycle')
                if len(payload)>artifact['cap']:raise ValueError('reserved PNG quota too small')
                disk=os.statvfs(path.parent)
                if disk.f_bavail*disk.f_frsize < c['budget']['min_free_bytes']+len(payload):raise OSError('PAUSED_DISK: PNG headroom')
                temp=png_path+'.tmp'
                fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                try:
                    with os.fdopen(fd,'wb') as out:out.write(payload);out.flush();os.fsync(out.fileno())
                    # link is no-clobber and same-filesystem; unlink temp after
                    # publication. Both names reference one charged inode.
                    os.link(temp,png_path,follow_symlinks=False);os.unlink(temp)
                    dfd=os.open(str(path.parent),os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(dfd)
                    finally:os.close(dfd)
                    ledger.ready(png_id,len(payload),hashlib.sha256(payload).hexdigest())
                except BaseException:raise
            print(json.dumps(summary))
    finally:ledger.close()
def generate(c,aid):
    with lock(pathlib.Path(c['ledger']['path']).parent/'quicklook.lock') as guard:
        if guard is None:return False
        return _generate(c,aid)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--artifact-id',required=True);args=p.parse_args()
    try:
        if generate(load(args.config)[0],args.artifact_id) is False:sys.exit(75)
    except Exception as e:print(type(e).__name__+': '+str(e),file=sys.stderr);sys.exit(2)
