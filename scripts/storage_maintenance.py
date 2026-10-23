#!/usr/bin/env python3
"""Independent upload/quicklook and retention workers. No database transaction
is held across a transfer, plot, hash, file lock wait, rename or unlink.
"""
import argparse,hashlib,json,os,pathlib,shutil,subprocess,sys,time,uuid
from solar_storage.config import load
from solar_storage.ledger import Ledger,utc_us,day_of,digest
from solar_storage.ownership import lock,artifact_lock,owned_path
from solar_storage.transfer import Rclone,safe_error
from solar_storage.logging import event

def report(c,level,message):
    # Terminal output is immediate even when file logging filters INFO records.
    # Message inputs here are fixed text, numeric counters, JSON-escaped names
    # and safe_error(), never raw child output/config/token values.
    stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    stream=sys.stderr if level=='ERROR' else sys.stdout
    try:print(stamp+' ['+level+'] '+message,file=stream,flush=True)
    except OSError:pass
    if c is None:return
    try:event(c,level,message)
    except Exception as e:
        # A log-file failure must not turn a verified upload into a FAILED job.
        try:print(stamp+' [WARNING] LOG FILE UNAVAILABLE: '+type(e).__name__,file=sys.stderr,flush=True)
        except OSError:pass

def file_label(a):return json.dumps(pathlib.Path(a['path']).name,ensure_ascii=True)

def target(c,a):return c['sync']['remote_name']+':'+c['sync']['remote_base_dir']+'/'+a['day']+'/'+pathlib.Path(a['path']).name
def destination(c):return c['sync']['remote_name']+':'+c['sync']['remote_base_dir']
def expired(c,a,now):return c['retention']['enabled'] and now-a['start_utc_us']>=c['retention']['days']*86400e6

def status(c,role,fields):
    path=pathlib.Path(c['status']['path']+'.'+role);path.parent.mkdir(parents=True,exist_ok=True)
    payload=json.dumps({'status_schema_version':1,'timestamp_utc_us':utc_us(),'worker':role,**fields},allow_nan=False).encode()
    if len(payload)>c['status']['max_bytes']:raise RuntimeError('status cap')
    temp=str(path)+'.tmp'
    with open(temp,'wb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    report(c,'WARNING' if fields.get('state','').startswith(('PAUSED','DEGRADED')) else 'INFO',role.upper()+' STATE: '+str(fields.get('state','unknown')))

def clock_safe(c,ledger,now):
    mono=time.monotonic();boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    trusted=False
    try:
        r=subprocess.run(['timedatectl','show','--property=NTPSynchronized','--value'],capture_output=True,text=True,timeout=c['ledger']['busy_timeout_sec'])
        trusted=r.returncode==0 and r.stdout.strip()=='yes'
    except (OSError,subprocess.TimeoutExpired):pass
    old=ledger.db.execute("SELECT value FROM meta WHERE key='cleanup_clock'").fetchone();old=json.loads(old[0]) if old else None
    jump=old and (old['boot']!=boot or abs((now-old['utc'])/1e6-(mono-old['mono']))>c['time']['clock_jump_threshold_sec'])
    if c['time']['cleanup_require_ntp_sync'] and not trusted:return False
    if jump and not (trusted and old.get('pending')):
        old['pending']=True
        with ledger.transaction() as db:db.execute("UPDATE meta SET value=? WHERE key='cleanup_clock'",(json.dumps(old),))
        return False
    value=json.dumps({'boot':boot,'utc':now,'mono':mono,'pending':False})
    with ledger.transaction() as db:db.execute("INSERT INTO meta VALUES('cleanup_clock',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(value,))
    return True

def recover(c,l):
    for a in l.rows():
        if a['state']!='WRITING':continue
        with lock(artifact_lock(c,a['id'])) as guard:
            if guard is None:continue
            path=owned_path(c,a['path'])
            # A final name is evidence of publication, but still validate shape
            # and cap. Upload verification is never inferred by recovery.
            if path.exists():
                try:
                    if path.stat().st_size>a['cap']:raise ValueError('cap')
                    if a['kind']=='h5':
                        from solar_storage.reader import open_final
                        with open_final(path):pass
                    else:
                        with open(path,'rb') as f:
                            if f.read(8)!=b'\x89PNG\r\n\x1a\n':raise ValueError('PNG signature')
                    l.ready(a['id'],path.stat().st_size,digest(path));continue
                except Exception:pass
            with l.transaction() as db:db.execute("UPDATE artifact SET state='QUARANTINED',generation=generation+1 WHERE id=? AND state='WRITING'",(a['id'],))

def cleanup(c,l,now=None,clock_check=True):
    now=utc_us() if now is None else now
    if not c['retention']['enabled']:
        status(c,'cleanup',{'state':'DISABLED'});return True
    if clock_check and not clock_safe(c,l,now):status(c,'cleanup',{'state':'PAUSED_TIME_QUALITY'});return False
    deleted=0;busy=0;lag=0.0
    try:identity=Rclone(c).identity()
    except Exception:identity=None
    for a in l.rows():
        if a['start_utc_us']>now+c['time']['max_future_skew_sec']*1e6:continue
        is_expired=expired(c,a,now)
        orphan=a['state']=='QUARANTINED' and now-a['created_utc_us']>=c['retention']['stale_temp_grace_sec']*1e6
        if not is_expired and not orphan and a['state']!='DELETE_PENDING':continue
        verified=l.job(a['id'],identity,'upload') if identity else None;verified=bool(verified and verified['status']=='VERIFIED' and verified['digest']==a['digest'] and a['digest'])
        if is_expired and not c['retention']['delete_unuploaded_expired'] and not verified and a['state']!='DELETE_PENDING':continue
        with lock(artifact_lock(c,a['id'])) as guard:
            if guard is None:busy+=1;continue
            # Recheck after ownership acquisition to reject a stale list result.
            current=l.db.execute('SELECT * FROM artifact WHERE id=?',(a['id'],)).fetchone()
            if not current or current['state']=='DELETED':continue
            a=dict(current);path=owned_path(c,a['path'])
            with l.transaction() as db:
                if a['state']!='DELETE_PENDING':
                    db.execute("UPDATE artifact SET state='DELETE_PENDING',generation=generation+1 WHERE id=?",(a['id'],))
                    size=max(a['size'],path.stat().st_size if path.exists() else 0)
                    db.execute('INSERT INTO audit(utc_us,artifact_id,start_utc_us,size,reason,upload_status) VALUES(?,?,?,?,?,?)',(now,a['id'],a['start_utc_us'],size,'RETENTION_EXPIRED' if is_expired else 'STALE_ORPHAN','VERIFIED' if verified else 'UNVERIFIED'))
                    if not verified:
                        for key,value in [('expired_unuploaded_files',1),('expired_unuploaded_bytes',size)]:db.execute('INSERT INTO counters VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value',(key,value))
                    limit=min(c['ledger']['audit_max_rows'],max(1,c['retention']['audit_max_bytes']//512))
                    db.execute('DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT ?)',(limit,))
            started=time.monotonic()
            for candidate in (path,pathlib.Path(str(path)+'.tmp')):
                owned_path(c,str(candidate))
                try:candidate.unlink()
                except FileNotFoundError:pass
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
            with l.transaction() as db:db.execute("UPDATE artifact SET state='DELETED' WHERE id=? AND state='DELETE_PENDING'",(a['id'],))
            deleted+=1;lag=max(lag,time.monotonic()-started)
        # A stale worker must recheck lifecycle after locking, so deleting this
        # empty lock inode cannot resurrect a deleted artifact.
        try:pathlib.Path(artifact_lock(c,a['id'])).unlink()
        except FileNotFoundError:pass
    # Keep a bounded tombstone set; older IDs are unique and never reused. Jobs
    # referencing removed rows cannot transition or publish.
    with l.transaction() as db:
        ids=[r[0] for r in db.execute("SELECT id FROM artifact WHERE state='DELETED' ORDER BY start_utc_us DESC LIMIT -1 OFFSET ?",(c['ledger']['tombstone_max_rows'],))]
        for aid in ids:db.execute('DELETE FROM job WHERE artifact_id=?',(aid,));db.execute('DELETE FROM artifact WHERE id=?',(aid,))
    status(c,'cleanup',{'state':'IDLE','deleted':deleted,'busy_expired':busy,'measured_max_unlink_fsync_sec':lag,'expired_unuploaded':dict(l.db.execute('SELECT key,value FROM counters'))})
    return True

def sync(c,l,config_path):
    started=time.monotonic()
    counts=dict(uploaded=0,upload_failed=0,already_verified=0,not_ready=0,
                too_young=0,expired=0,retry_wait=0,retry_exhausted=0,busy=0,
                quicklook_ok=0,quicklook_failed=0)
    report(c,'INFO','SYNC START: upload_enabled='+str(c['sync']['enabled']))
    transport=Rclone(c);identity=transport.identity() if c['sync']['enabled'] else None
    now=utc_us();backlog=0;last_verified=0
    for a in l.rows():
        if a['state']!='READY':counts['not_ready']+=1;continue
        if expired(c,a,now):counts['expired']+=1;continue
        if now-a['created_utc_us']<c['sync']['min_age_sec']*1e6:counts['too_young']+=1;continue
        path=owned_path(c,a['path']);label=file_label(a)
        if a['kind']=='h5' and a['quicklook_enabled']:
            job=l.job(a['id'],'snapshot','quicklook');q=c['quicklook']
            if not job or (job['status']!='VERIFIED' and job['attempts']<q['max_attempts'] and job['next_utc_us']<=now):
                try:
                    report(c,'INFO','QUICKLOOK START: '+label)
                    result=subprocess.run([sys.executable,str(pathlib.Path(__file__).with_name('generate_quicklook.py')),'--config',config_path,'--artifact-id',a['id']],capture_output=True,timeout=c['retention']['reader_timeout_sec'])
                    if result.returncode not in (0,75):raise RuntimeError('quicklook_exit_'+str(result.returncode))
                    if result.returncode==0:
                        l.set_job(a,'snapshot','quicklook','VERIFIED');counts['quicklook_ok']+=1
                        report(c,'INFO','QUICKLOOK OK: '+label+'; new PNG is considered by the next sync pass')
                    else:report(c,'INFO','QUICKLOOK SKIP: '+label+'; generator busy')
                except Exception as e:
                    detail=safe_error(e);l.set_job(a,'snapshot','quicklook','FAILED',detail)
                    counts['quicklook_failed']+=1;report(c,'ERROR','QUICKLOOK FAILED: '+label+'; '+detail)
        if not c['sync']['enabled']:continue
        job=l.job(a['id'],identity,'upload')
        if job and job['status']=='VERIFIED' and job['digest']==a['digest']:
            with lock(artifact_lock(c,a['id']),exclusive=False) as guard:
                if guard is None:counts['busy']+=1;continue
                if path.stat().st_size==a['size'] and digest(path)==a['digest']:
                    last_verified=max(last_verified,job['verified_utc_us']);counts['already_verified']+=1;continue
                with l.transaction() as db:db.execute("UPDATE artifact SET state='QUARANTINED',generation=generation+1 WHERE id=? AND state='READY'",(a['id'],))
                report(c,'ERROR','UPLOAD SKIP: '+label+'; local file changed after verification; QUARANTINED')
                continue
        backlog+=1
        if job and job['attempts']>=c['sync']['max_attempts']:
            counts['retry_exhausted']+=1
            report(c,'WARNING','UPLOAD SKIP: '+label+'; RETRY LIMIT attempts='+str(job['attempts'])+'/'+str(c['sync']['max_attempts']))
            continue
        if job and job['next_utc_us']>now:
            counts['retry_wait']+=1
            report(c,'INFO','UPLOAD WAIT: '+label+'; backoff_remaining_sec='+str(round((job['next_utc_us']-now)/1e6,1)))
            continue
        with lock(artifact_lock(c,a['id']),exclusive=False) as guard:
            if guard is None:counts['busy']+=1;continue
            row=l.db.execute('SELECT * FROM artifact WHERE id=?',(a['id'],)).fetchone()
            if not row or row['state']!='READY' or expired(c,dict(row),utc_us()):continue
            a=dict(row);stage='local validation';attempt_start=time.monotonic()
            try:
                size=path.stat().st_size
                if size>a['cap'] or size!=a['size']:raise RuntimeError('local artifact size changed')
                sha=digest(path)
                if a['digest'] and sha!=a['digest']:raise RuntimeError('local artifact digest changed')
                if not a['digest']:
                    with l.transaction() as db:db.execute("UPDATE artifact SET digest=? WHERE id=? AND state='READY'",(sha,a['id']))
                    a['digest']=sha
                attempt=job['attempts']+1 if job else 1
                report(c,'INFO','UPLOAD START: '+label+'; bytes='+str(size)+'; attempt='+str(attempt)+'/'+str(c['sync']['max_attempts']))
                stage='copy';transport.copy(path,target(c,a),c['sync']['timeout_sec'])
                report(c,'INFO','UPLOAD COPIED: '+label+'; verifying remote size and SHA-256')
                stage='verification';transport.verify(target(c,a),size,sha,c['sync']['timeout_sec'])
                stage='post-verification'
                if digest(path)!=sha:raise RuntimeError('local artifact changed during upload')
                if transport.identity()!=identity:raise RuntimeError('destination identity changed during upload')
                l.set_job(a,identity,'upload','VERIFIED',sha=sha);last_verified=utc_us();counts['uploaded']+=1
                report(c,'INFO','UPLOAD OK: '+label+'; VERIFIED size+SHA256; elapsed_sec='+str(round(time.monotonic()-attempt_start,2)))
            except Exception as e:
                detail=safe_error(e);l.set_job(a,identity,'upload','FAILED',detail);counts['upload_failed']+=1
                report(c,'ERROR','UPLOAD FAILED: '+label+'; stage='+stage+'; '+detail)
    failures=l.db.execute("SELECT COUNT(*) FROM job WHERE status='FAILED'").fetchone()[0]
    oldest=l.db.execute("SELECT MIN(start_utc_us) FROM artifact WHERE state='READY'").fetchone()[0]
    status(c,'sync',{'state':'DEGRADED' if failures else 'IDLE','failed_jobs':failures,'upload_backlog':backlog,'oldest_ready_age_sec':max(0,(utc_us()-oldest)/1e6) if oldest else 0,'last_verified_upload_utc_us':last_verified,'last_run':counts})
    report(c,'INFO','SYNC SUMMARY: '+json.dumps(counts,sort_keys=True)+'; failed_jobs_all_destinations='+str(failures)+'; elapsed_sec='+str(round(time.monotonic()-started,2)))

def probe(c,l):
    if not c['startup_check']['enabled']:
        report(c,'INFO','STARTUP PROBE DISABLED by config');return
    p=c['startup_check'];transport=Rclone(c);pid=uuid.uuid4().hex
    directory=pathlib.Path(c['ledger']['path']).parent
    path=directory/('probe-'+pid+'.bin');remote=destination(c)+'/'+p['remote_probe_dir']+'/probe-'+pid+'.bin'
    with l.transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS probe(id TEXT PRIMARY KEY,path TEXT NOT NULL,remote TEXT NOT NULL,created_utc_us INTEGER NOT NULL)')
        db.execute('INSERT INTO probe VALUES(?,?,?,?)',(pid,str(path),remote,utc_us()))
    payload=os.urandom(p['probe_size_bytes']);sha=hashlib.sha256(payload).hexdigest()
    with open(path,'xb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
    try:
        for attempt in range(p['max_attempts']):
            try:
                report(c,'INFO','STARTUP PROBE START: attempt='+str(attempt+1)+'/'+str(p['max_attempts']))
                transport.copy(path,remote,p['timeout_sec']);transport.verify(remote,len(payload),sha,p['timeout_sec'])
                if p['remove_probe_after_success']:
                    transport.remove_probe(remote,p['timeout_sec'])
                    with l.transaction() as db:db.execute('DELETE FROM probe WHERE id=?',(pid,))
                status(c,'startup',{'state':'PROBE_PASSED','checked_utc_us':utc_us()});return
            except Exception as e:
                detail=safe_error(e)
                report(c,'ERROR','STARTUP PROBE FAILED: attempt='+str(attempt+1)+'; '+detail)
                if attempt+1==p['max_attempts']:
                    status(c,'startup',{'state':'PROBE_FAILED','error':detail})
                    raise RuntimeError('startup probe failed; SDR must remain off') from None
                time.sleep(min(p['backoff_max_sec'],p['backoff_initial_sec']*2**attempt))
    finally:
        path.unlink(missing_ok=True)
        # Keep remote probe identity for crash/failure cleanup. The record is
        # removed only when removal succeeded or a later cleanup verifies it.

def stale_probes(c,l):
    if not l.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='probe'").fetchone():return
    prefix=destination(c)+'/'+c['startup_check']['remote_probe_dir']+'/probe-';transport=Rclone(c)
    for row in l.db.execute('SELECT * FROM probe').fetchall():
        if utc_us()-row['created_utc_us']<c['startup_check']['stale_probe_grace_sec']*1e6:continue
        if row['remote']!=prefix+row['id']+'.bin':continue
        try:
            transport.remove_probe(row['remote'],c['startup_check']['timeout_sec'])
            local=pathlib.Path(row['path']);expected=pathlib.Path(c['ledger']['path']).parent/('probe-'+row['id']+'.bin')
            if local==expected and not local.is_symlink():local.unlink(missing_ok=True)
            with l.transaction() as db:db.execute('DELETE FROM probe WHERE id=?',(row['id'],))
            report(c,'INFO','STALE PROBE REMOVED')
        except Exception as e:report(c,'WARNING','STALE PROBE CLEANUP FAILED: '+safe_error(e))

def run(role,path):
    c,_=load(path)
    if role=='startup':
        data=pathlib.Path(c['storage']['data_dir']);data.mkdir(parents=True,exist_ok=True)
        owner_path=data/'.solar_storage_owner.json'
        ledger_path=pathlib.Path(c['ledger']['path'])
        if owner_path.is_symlink():raise RuntimeError('symlink ownership marker')
        if owner_path.exists():
            owner=json.loads(owner_path.read_text())
            if owner['ledger_path']!=str(ledger_path) or not ledger_path.exists():raise RuntimeError('ledger moved/missing: offline migration required, quota must not reset')
        elif not ledger_path.exists() and any(data.iterdir()):raise RuntimeError('nonempty data_dir has no ledger ownership; refusing quota reset')
        l=Ledger(c,initialize=True)
        try:
            with l.transaction() as db:
                uid=db.execute("SELECT value FROM meta WHERE key='ledger_uuid'").fetchone()
                if uid is None:
                    value=uuid.uuid4().hex;db.execute("INSERT INTO meta VALUES('ledger_uuid',?)",(value,))
                else:value=uid[0]
            if owner_path.exists():
                if json.loads(owner_path.read_text())['ledger_uuid']!=value:raise RuntimeError('ledger identity mismatch')
            else:
                with owner_path.open('x') as f:json.dump({'ledger_path':str(ledger_path),'ledger_uuid':value},f);f.flush();os.fsync(f.fileno())
            with l.transaction() as db:l.quota(db,day_of(utc_us(),c))
            recover(c,l)
            # A previous startup cannot still own a local probe while this
            # caller owns the application's SingleInstanceLock.
            if l.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='probe'").fetchone():
                for old in l.db.execute('SELECT id,path FROM probe').fetchall():
                    expected=pathlib.Path(c['ledger']['path']).parent/('probe-'+old['id']+'.bin')
                    if pathlib.Path(old['path'])==expected and not expected.is_symlink():expected.unlink(missing_ok=True)
            with lock(c['retention']['lock_path']) as guard:
                if guard is None:raise RuntimeError('cleanup worker busy during startup')
                if not cleanup(c,l):raise RuntimeError('cleanup paused by time quality; no hardware startup')
            if shutil.disk_usage(data).free<c['budget']['min_free_bytes']:raise RuntimeError('local disk below stop threshold')
            probe(c,l)
        finally:l.close()
        return
    lockpath=c['retention']['lock_path'] if role=='cleanup' else c['sync']['lock_path']
    with lock(lockpath) as guard:
        if guard is None:
            report(c,'INFO',role.upper()+' SKIP: another worker holds the lock');return
        l=Ledger(c)
        try:
            recover(c,l)
            if role=='cleanup':cleanup(c,l)
            elif role=='sync':sync(c,l,str(pathlib.Path(path).resolve()));stale_probes(c,l)
        finally:l.close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('role',choices=['startup','sync','cleanup']);p.add_argument('--config',required=True);args=p.parse_args()
    try:run(args.role,args.config)
    except Exception as e:
        report(None,'ERROR',args.role.upper()+' FAILED: '+safe_error(e));sys.exit(2)
