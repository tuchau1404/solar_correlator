"""Version 1 transaction contract. Charged caps are conservative, never refunded.
File locks, not database transactions, protect long-running readers/writers.
"""
import contextlib,datetime,hashlib,os,pathlib,sqlite3,time
from zoneinfo import ZoneInfo
SCHEMA=pathlib.Path(__file__).resolve().parents[2]/'config'/'storage_ledger_v1.sql'
def utc_us():return time.time_ns()//1000
def day_of(us,c):return datetime.datetime.fromtimestamp(us/1e6,ZoneInfo(c['time']['observation_timezone'])).strftime('%Y-%m-%d')
def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        while b:=f.read(1024*1024):h.update(b)
    return h.hexdigest()
class Ledger:
    def __init__(self,c,initialize=False):
        self.c=c;p=pathlib.Path(c['ledger']['path']);p.parent.mkdir(parents=True,exist_ok=True)
        new=not p.exists()
        if not new and p.stat().st_size>c["ledger"]["max_bytes"]:raise RuntimeError("existing ledger exceeds configured cap")
        if new and not initialize:raise RuntimeError('ledger missing: run startup orchestration')
        self.db=sqlite3.connect(p,timeout=c['ledger']['busy_timeout_sec'],isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA max_page_count='+str(c['ledger']['max_bytes']//self.db.execute('PRAGMA page_size').fetchone()[0]))
        version=self.db.execute('PRAGMA user_version').fetchone()[0]
        if new and version==0 and initialize:self.db.executescript(SCHEMA.read_text());version=1
        if version!=1:raise RuntimeError(f'unsupported ledger_version={version}; offline migration required')
        required={'meta','quota_day','artifact','job','audit','counters'}
        present={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required<=present:raise RuntimeError('incomplete ledger schema; offline recovery required')
        if self.db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('ledger integrity failure')
    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:yield self.db;self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
    def quota(self,db,day):
        row=db.execute('SELECT * FROM quota_day WHERE day=?',(day,)).fetchone()
        if row:return row
        high=db.execute("SELECT value FROM meta WHERE key='day_highwater'").fetchone()
        if high and day<high[0]:raise RuntimeError('quota day moved backwards or retired: paused')
        aux=2*self.c['ledger']['max_bytes']+self.c['budget']['metadata_headroom_bytes']
        db.execute('INSERT INTO quota_day(day,aux_committed) VALUES(?,?)',(day,aux))
        db.execute("INSERT INTO meta VALUES('day_highwater',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(day,))
        return db.execute('SELECT * FROM quota_day WHERE day=?',(day,)).fetchone()
    def prepare_png(self,parent,png_id,path):
        with self.transaction() as db:
            p=db.execute('SELECT * FROM artifact WHERE id=?',(parent,)).fetchone()
            if not p or p['state']!='READY' or not p['quicklook_enabled']:raise RuntimeError('parent not eligible')
            existing=db.execute('SELECT * FROM artifact WHERE id=?',(png_id,)).fetchone()
            if existing:return dict(existing)
            if p["png_cap"]<=0:raise RuntimeError("PNG reservation already consumed; no resurrection")
            db.execute('INSERT INTO artifact(id,parent_id,path,day,start_utc_us,created_utc_us,cap,png_cap,state,kind,quicklook_enabled,config_hash,session) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(png_id,parent,path,p['day'],p['start_utc_us'],utc_us(),p['png_cap'],0,'WRITING','png',0,p['config_hash'],p['session']))
            db.execute('UPDATE artifact SET png_cap=0 WHERE id=?',(parent,))
            db.execute('UPDATE quota_day SET png_reserved=png_reserved-?,png_committed=png_committed+? WHERE day=?',(p['png_cap'],p['png_cap'],p['day']))
            return dict(db.execute('SELECT * FROM artifact WHERE id=?',(png_id,)).fetchone())
    def ready(self,aid,size,sha):
        with self.transaction() as db:
            row=db.execute('SELECT cap,state FROM artifact WHERE id=?',(aid,)).fetchone()
            if not row or size>row['cap'] or row['state']!='WRITING':raise RuntimeError('invalid finalize transition')
            db.execute("UPDATE artifact SET state='READY',size=?,digest=? WHERE id=?",(size,sha,aid))
    def rows(self):return [dict(r) for r in self.db.execute("SELECT * FROM artifact WHERE state NOT IN ('DELETED') ORDER BY start_utc_us,id")]
    def job(self,aid,destination,kind):
        row=self.db.execute('SELECT * FROM job WHERE artifact_id=? AND destination=? AND kind=?',(aid,destination,kind)).fetchone();return dict(row) if row else None
    def set_job(self,a,destination,kind,status,error='',sha=''):
        with self.transaction() as db:
            current=db.execute('SELECT state,generation FROM artifact WHERE id=?',(a['id'],)).fetchone()
            if not current or current['state']!='READY' or current['generation']!=a['generation']:return False
            old=self.job(a['id'],destination,kind);attempts=(old['attempts'] if old else 0)+1
            cfg=self.c['sync'] if kind=='upload' else self.c['quicklook']
            delay=min(cfg['backoff_max_sec'],cfg['backoff_initial_sec']*2**min(attempts-1,30))
            db.execute('INSERT INTO job VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(artifact_id,destination,kind) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,next_utc_us=excluded.next_utc_us,error=excluded.error,verified_utc_us=excluded.verified_utc_us,digest=excluded.digest',(a['id'],destination,kind,status,attempts,utc_us()+int(delay*1e6),error[:1000],utc_us() if status=='VERIFIED' else 0,sha))
            return True
    def close(self):self.db.close()
