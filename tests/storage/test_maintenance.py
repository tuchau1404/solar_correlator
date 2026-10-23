import copy,hashlib,importlib.util,json,multiprocessing,os,pathlib,sqlite3,subprocess,sys,tempfile,unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
from solar_storage.config import load
from solar_storage.ledger import Ledger,utc_us,day_of
from solar_storage.ownership import lock,artifact_lock
from solar_storage.science import derived
import storage_maintenance as maintenance

class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
        self.raw=json.loads((ROOT/'config/solar_storage.json').read_text());c=self.raw
        for g,k in [('storage','data_dir'),('ledger','path'),('ledger','migration_lock_path'),('sync','lock_path'),('sync','manifest_path'),('retention','lock_path'),('logging','path'),('status','path')]:c[g][k]=str(self.root/('data' if k=='data_dir' else 'state/'+pathlib.Path(c[g][k]).name))
        c['startup_check']['enabled']=False;c['sync']['enabled']=False;c['time']['cleanup_require_ntp_sync']=False;c['budget']['min_free_bytes']=1;c['budget']['resume_free_bytes']=2
        self.path=self.root/'config.json';self.path.write_text(json.dumps(c));self.c,self.hash=load(self.path);pathlib.Path(c['storage']['data_dir']).mkdir();self.l=Ledger(self.c,True);self.now=utc_us()
    def tearDown(self):self.l.close();self.tmp.cleanup()
    def artifact(self,aid='abc',start=None,state='READY'):
        start=self.now if start is None else start;path=pathlib.Path(self.c['storage']['data_dir'])/(aid+'.h5');path.write_bytes(b'data');day=day_of(start,self.c)
        with self.l.transaction() as db:
            self.l.quota(db,day)
            db.execute('UPDATE quota_day SET hdf5_committed=hdf5_committed+4 WHERE day=?',(day,))
            db.execute('INSERT INTO artifact(id,path,day,start_utc_us,created_utc_us,cap,png_cap,state,kind,size,digest,quicklook_enabled,config_hash,session) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(aid,str(path),day,start,start,4,0,state,'h5',4,hashlib.sha256(b'data').hexdigest(),0,self.hash,'abc'))
        return path
    def test_config_validation(self):
        for change in [lambda c:c['storage'].update({'unknown':1}),lambda c:c['retention'].update(days=0),lambda c:c['startup_check'].update(enabled=True),lambda c:c['budget'].update(resume_free_bytes=1),lambda c:c['config_version'].__class__]:
            c=copy.deepcopy(self.raw);change(c)
            if c==self.raw:continue
            self.path.write_text(json.dumps(c))
            with self.assertRaises(ValueError):load(self.path)
        self.path.write_text('{"config_version":1,"config_version":1}')
        with self.assertRaises(ValueError):load(self.path)
    def test_retention_boundary_nonrefund(self):
        p=self.artifact(start=self.now-int(5*86400e6));before=self.l.db.execute('SELECT hdf5_committed FROM quota_day').fetchone()[0]
        maintenance.cleanup(self.c,self.l,self.now-1,False);self.assertTrue(p.exists())
        maintenance.cleanup(self.c,self.l,self.now,False);self.assertFalse(p.exists())
        self.assertEqual(self.l.db.execute('SELECT hdf5_committed FROM quota_day').fetchone()[0],before)
        self.assertEqual(self.l.db.execute("SELECT value FROM counters WHERE key='expired_unuploaded_files'").fetchone()[0],1)
        maintenance.cleanup(self.c,self.l,self.now+1,False)
        self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],1)
    def test_active_reader_and_changed_retention(self):
        p=self.artifact(start=self.now-int(6*86400e6))
        with lock(artifact_lock(self.c,'abc'),False):maintenance.cleanup(self.c,self.l,self.now,False);self.assertTrue(p.exists())
        self.c['retention']['days']=7;maintenance.cleanup(self.c,self.l,self.now,False);self.assertTrue(p.exists())
        self.c['retention']['days']=5;maintenance.cleanup(self.c,self.l,self.now,False);self.assertFalse(p.exists())
    def test_delete_pending_recovery(self):
        p=self.artifact(start=self.now-int(6*86400e6),state='DELETE_PENDING');p.unlink()
        maintenance.cleanup(self.c,self.l,self.now,False)
        self.assertEqual(self.l.db.execute('SELECT state FROM artifact').fetchone()[0],'DELETED')
    def test_unverified_protection_option(self):
        p=self.artifact(start=self.now-int(6*86400e6));self.c['retention']['delete_unuploaded_expired']=False
        maintenance.cleanup(self.c,self.l,self.now,False);self.assertTrue(p.exists())
    def test_symlink_not_followed(self):
        p=self.artifact(start=self.now-int(6*86400e6));external=self.root/'keep';external.write_text('keep');p.unlink();p.symlink_to(external)
        with self.assertRaises(RuntimeError):maintenance.cleanup(self.c,self.l,self.now,False)
        self.assertEqual(external.read_text(),'keep')
    def test_clock_forward_pause(self):
        p=self.artifact(start=self.now-int(6*86400e6))
        class Reply:returncode=1;stdout='no'
        with patch.object(maintenance.subprocess,'run',return_value=Reply()):
            self.assertTrue(maintenance.clock_safe(self.c,self.l,self.now))
            self.assertFalse(maintenance.cleanup(self.c,self.l,self.now+int(86400e6),True))
        self.assertTrue(p.exists())
    def test_unknown_ledger_version(self):
        self.l.db.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError):Ledger(self.c)
    def test_no_quota_reissue_backwards(self):
        with self.l.transaction() as db:self.l.quota(db,'2026-10-17')
        with self.assertRaises(RuntimeError):
            with self.l.transaction() as db:self.l.quota(db,'2026-10-16')
    def test_stale_job_cannot_resurrect(self):
        self.artifact();a=self.l.rows()[0];self.l.db.execute("UPDATE artifact SET state='DELETED',generation=generation+1")
        self.assertFalse(self.l.set_job(a,'remote:dir','upload','VERIFIED',sha=a['digest']))
        self.assertFalse(self.l.job('abc','remote:dir','upload'))
    def test_remote_identity(self):
        self.artifact();a=self.l.rows()[0];self.l.set_job(a,'one:dir','upload','VERIFIED',sha=a['digest'])
        self.assertIsNone(self.l.job('abc','two:dir','upload'))
    def test_probe_pass_fail(self):
        c=copy.deepcopy(self.c);c['startup_check']['enabled']=True;c['startup_check']['max_attempts']=1
        class Fake:
            def __init__(self,c):pass
            def copy(self,p,t,timeout):self.data=pathlib.Path(p).read_bytes()
            def verify(self,t,size,sha,timeout):assert len(self.data)==size and hashlib.sha256(self.data).hexdigest()==sha
            def remove_probe(self,t,timeout):pass
        with patch.object(maintenance,'Rclone',Fake):maintenance.probe(c,self.l)
        self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM probe').fetchone()[0],0)
        class Bad(Fake):
            def verify(self,*args):raise RuntimeError('checksum')
        with patch.object(maintenance,'Rclone',Bad):
            with self.assertRaisesRegex(RuntimeError,'SDR must remain off'):maintenance.probe(c,self.l)
        self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM probe').fetchone()[0],1)
    def test_science(self):
        import numpy as np
        mag,phase,g,g2,bad=derived(np.array([4.,1.]),np.array([9.,1.]),np.array([1.8,0]),np.array([2.4,0]))
        self.assertAlmostEqual(g[0],.5);self.assertAlmostEqual(g2[0],.25);self.assertTrue(np.isnan(phase[1]));self.assertFalse(bad.any())
    def test_destination_credentials_identity(self):
        from solar_storage.transfer import Rclone
        conf=self.root/'rclone.conf';self.c['sync']['rclone_config_path']=str(conf)
        def write(access,refresh):conf.write_text('[gdrive]\ntype = drive\ntoken = '+json.dumps({'access_token':access,'refresh_token':refresh,'expiry':'irrelevant'})+'\n')
        write('first','account-one');a=Rclone(self.c).identity()
        write('renewed','account-one');self.assertEqual(a,Rclone(self.c).identity())
        write('renewed','account-two');self.assertNotEqual(a,Rclone(self.c).identity())
    def test_two_process_png_reservation(self):
        self.artifact();self.l.db.execute('UPDATE artifact SET png_cap=1000000,quicklook_enabled=1');self.l.db.execute('UPDATE quota_day SET png_reserved=1000000')
        code="import sys;sys.path.insert(0,sys.argv[1]);from solar_storage.config import load;from solar_storage.ledger import Ledger;l=Ledger(load(sys.argv[2])[0]);l.prepare_png('abc','abc_a',sys.argv[3]);l.close()"
        args=[sys.executable,'-c',code,str(ROOT/'scripts'),str(self.path),str(pathlib.Path(self.c['storage']['data_dir'])/'abc.png')]
        processes=[subprocess.Popen(args) for _ in range(2)]
        self.assertEqual([p.wait() for p in processes],[0,0])
        row=self.l.db.execute('SELECT png_reserved,png_committed FROM quota_day').fetchone()
        self.assertEqual(tuple(row),(0,1000000));self.assertEqual(self.l.db.execute("SELECT COUNT(*) FROM artifact WHERE kind='png'").fetchone()[0],1)
    def test_after_publish_recovery(self):
        import h5py,numpy as np
        from solar_storage.reader import ARRAYS,SCALARS
        path=self.artifact(state='WRITING');self.l.db.execute("UPDATE artifact SET cap=1000000,size=0,digest=''")
        with h5py.File(path,'w') as f:
            g=f.create_group('metadata');g.attrs['data_schema_version']=1;g.attrs['units']='ADC-count^2 per input FFT bin (mean after frequency grouping)';g.attrs['cross_product_convention']='X1*conj(X2) * exp(-j*phi)'
            f['frequency_hz']=[1.,2.]
            for name in ARRAYS:f['data/'+name]=np.ones((1,2))
            for name in SCALARS:f['data/'+name]=[1]
        maintenance.recover(self.c,self.l)
        row=self.l.db.execute('SELECT state,digest FROM artifact').fetchone();self.assertEqual(row[0],'READY');self.assertTrue(row[1])
        self.assertFalse(self.l.job('abc','somewhere','upload'))
if __name__=='__main__':unittest.main()
