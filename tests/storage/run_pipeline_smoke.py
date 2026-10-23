#!/usr/bin/env python3
"""Fresh offline fixture for the native pipeline; never contacts an SDR/remote."""
import argparse,json,pathlib,sqlite3,subprocess,tempfile,sys
import h5py,numpy as np
ROOT=pathlib.Path(__file__).resolve().parents[2]
p=argparse.ArgumentParser();p.add_argument('--build-dir',required=True);p.add_argument('--deferred',action='store_true');a=p.parse_args()
with tempfile.TemporaryDirectory(prefix='solar-smoke-') as td:
    root=pathlib.Path(td);c=json.loads((ROOT/'config/solar_storage.json').read_text())
    c['storage']['data_dir']=str(root/'data')
    for group,key in [('ledger','path'),('ledger','migration_lock_path'),('sync','lock_path'),('sync','manifest_path'),('retention','lock_path'),('logging','path'),('status','path')]:
        c[group][key]=str(root/'state'/pathlib.Path(c[group][key]).name)
    c['startup_check']['enabled']=False;c['sync']['enabled']=False;c['time']['cleanup_require_ntp_sync']=False
    c['budget']['min_free_bytes']=1;c['budget']['resume_free_bytes']=2
    cfg=root/'config.json';cfg.write_text(json.dumps(c))
    subprocess.run([sys.executable,'-B',str(ROOT/'scripts/storage_maintenance.py'),'startup','--config',str(cfg)],check=True)
    subprocess.run([str(pathlib.Path(a.build_dir).resolve()/'test_storage_pipeline'),str(cfg)]+(['deferred'] if a.deferred else []),check=True)
    db=sqlite3.connect(c['ledger']['path']);db.row_factory=sqlite3.Row
    rows=db.execute("SELECT * FROM artifact WHERE kind='h5'").fetchall();assert len(rows)==1 and rows[0]['state']=='READY'
    with h5py.File(rows[0]['path']) as f:
        
        if a.deferred:
            meta=f['metadata'].attrs
            assert json.loads(meta['aligned']) is True and json.loads(meta['phase_calibrated']) is True
            assert int(meta['calibration_id'])==1
            np.testing.assert_allclose(f['calibration_phi_rad_unshifted'][:],0.25)
        d=f['data'];assert d['s11_linear'].shape==(10,512)
        assert len({len(x) for x in d.values()})==1
        for name,value in [('s11_linear',4),('s22_linear',9),('s12_real',1.8),('s12_imag',2.4)]:np.testing.assert_allclose(d[name][:],value,rtol=1e-6)
        np.testing.assert_array_equal(d['valid_fft_frames'][:],11718)
        assert np.all(np.diff(f['frequency_hz'][:])>0)
    subprocess.run([sys.executable,'-B',str(ROOT/'scripts/generate_quicklook.py'),'--config',str(cfg),'--artifact-id',rows[0]['id']],check=True)
    png=db.execute("SELECT * FROM artifact WHERE kind='png'").fetchone();assert png['state']=='READY' and png['size']<=c['quicklook']['max_png_bytes']
    stat=json.loads(pathlib.Path(c['status']['path']).read_text());assert stat['source_dropped_records']==0 and stat['error']==''
    print('PASS native queue -> aggregation -> capped HDF5 -> SQLite -> quicklook PNG; 60 sources, 10 rows')
