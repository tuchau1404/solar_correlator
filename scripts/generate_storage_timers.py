#!/usr/bin/env python3
"""Generate maintenance timers from the sole operating config. Does not install."""
import argparse,json,pathlib
from solar_storage.config import load
p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--output-dir',required=True);a=p.parse_args()
c,_=load(a.config);cfg=str(pathlib.Path(a.config).resolve());out=pathlib.Path(a.output_dir);out.mkdir(parents=True,exist_ok=True)
def quote(x):return json.dumps(str(x)).replace('%','%%')
script=pathlib.Path(__file__).resolve().with_name('storage_maintenance.py')
for role,interval in [('sync',c['sync']['interval_sec']),('cleanup',c['retention']['cleanup_interval_sec'])]:
    service='solar-storage-'+role
    command=' '.join(quote(x) for x in ('/usr/bin/python3',script,role,'--config',cfg))
    (out/(service+'.service')).write_text(f'[Unit]\nDescription=Solar storage {role}\n\n[Service]\nType=oneshot\nExecStart={command}\nTimeoutStartSec=infinity\n')
    (out/(service+'.timer')).write_text(f'[Unit]\nDescription=Solar storage {role} schedule\n\n[Timer]\nOnBootSec={interval}s\nOnUnitInactiveSec={interval}s\nUnit={service}.service\n\n[Install]\nWantedBy=timers.target\n')
print(out)
