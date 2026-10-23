#!/usr/bin/env python3
import argparse,json,pathlib,time
from solar_storage.config import load
p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args();c,_=load(a.config)
result={}
for role,suffix in [('storage',''),('sync','.sync'),('cleanup','.cleanup'),('startup','.startup')]:
    path=pathlib.Path(c['status']['path']+suffix)
    try:
        if path.stat().st_size>c['status']['max_bytes']:raise ValueError('oversized status')
        s=json.loads(path.read_text())
        if s['status_schema_version']!=1:raise ValueError('unsupported status version')
        age=(time.time_ns()//1000-s['timestamp_utc_us'])/1e6
        s['freshness']='CURRENT' if 0<=age<=c['status']['stale_after_sec'] else 'STALE_OR_FUTURE'
        s['age_sec']=age;result[role]=s
    except Exception as e:result[role]={'freshness':'UNAVAILABLE','error':type(e).__name__}
print(json.dumps(result,indent=2))
