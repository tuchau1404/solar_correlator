#!/usr/bin/env python3
"""Release 1: offline checked backup of schema 1 only. Future versions rejected.
No schema change is pretended for a format that did not previously exist.
"""
import argparse,contextlib,pathlib,sqlite3
from solar_storage.config import load
from solar_storage.ledger import Ledger
from solar_storage.ownership import lock,artifact_lock
p=argparse.ArgumentParser();p.add_argument('--config',required=True);args=p.parse_args();c,_=load(args.config)
with contextlib.ExitStack() as stack:
    for path in ['/tmp/solar_correlator.lock',c['ledger']['migration_lock_path'],c['sync']['lock_path'],c['retention']['lock_path']]:
        if stack.enter_context(lock(path)) is None:raise SystemExit('Stop writer and all maintenance before offline ledger maintenance')
    l=Ledger(c);stack.callback(l.close)
    for a in l.rows():
        if stack.enter_context(lock(artifact_lock(c,a['id']))) is None:raise SystemExit('Artifact still owned by a worker')
    backup=pathlib.Path(c['ledger']['path']+'.backup')
    if backup.exists():raise SystemExit('Backup already exists; inspect/archive it before creating another')
    target=sqlite3.connect(backup)
    try:
        l.db.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or target.execute('PRAGMA user_version').fetchone()[0]!=1:raise RuntimeError('backup validation failed')
    finally:target.close()
    if backup.stat().st_size>c['ledger']['max_bytes']:raise RuntimeError('backup exceeds reserved database cap')
    print('Validated schema 1 backup:',backup)
