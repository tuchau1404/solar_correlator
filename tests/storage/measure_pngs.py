#!/usr/bin/env python3
"""Complete the 24h synthetic fixture's plots; refuses non-synthetic inputs."""
import contextlib,io,json,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
from solar_storage.config import load
from solar_storage.ledger import Ledger
from solar_storage.reader import open_final,text
from generate_quicklook import generate
c,_=load(sys.argv[1]);l=Ledger(c)
rows=[a for a in l.rows() if a['kind']=='h5' and a['state']=='READY']
for a in rows:
    with open_final(a['path']) as f:
        if text(f['metadata'].attrs['patch_state'])!='synthetic experiment':raise SystemExit('Refusing non-synthetic measurement input')
for i,a in enumerate(rows):
    with contextlib.redirect_stdout(io.StringIO()):generate(c,a['id'])
    if (i+1)%10==0:print(f'Generated {i+1}/{len(rows)} PNGs',flush=True)
result={}
for kind in ('h5','png'):
    files=[a for a in l.rows() if a['kind']==kind and a['state']=='READY'];result[kind+'_count']=len(files);result[kind+'_logical_bytes']=sum(pathlib.Path(a['path']).stat().st_size for a in files)
state=pathlib.Path(c['ledger']['path']).parent
result['auxiliary_live_logical_bytes']=sum(p.stat().st_size for p in state.rglob('*') if p.is_file())
result['actual_live_total_bytes']=result['h5_logical_bytes']+result['png_logical_bytes']+result['auxiliary_live_logical_bytes']
result['quota']=dict(l.db.execute('SELECT * FROM quota_day').fetchone());l.close()
assert result['h5_count']==97 and result['png_count']==97,result
assert result['actual_live_total_bytes']<=c['budget']['daily_limit_bytes'],result
print(json.dumps(result,indent=2))
