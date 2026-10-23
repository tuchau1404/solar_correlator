#!/usr/bin/env python3
"""Main-owned sequential scheduler. Linux process group includes all helpers."""
import argparse, ctypes, os, signal, time
from solar_storage.config import load
from solar_storage.ownership import lock
from storage_maintenance import run, report
from solar_storage.transfer import safe_error

class StopWorker(BaseException):
    pass

def stop(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Also stop a running quicklook/rclone if the parent died unexpectedly.
    if os.getpgrp() == os.getpid():
        os.killpg(os.getpgrp(), signal.SIGTERM)
    raise StopWorker()

def serve(path, parent):
    if os.getpgrp() != os.getpid():
        raise RuntimeError('automatic worker requires its own process group')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    libc=ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'parent death signal')
    if os.getppid() != parent:
        raise StopWorker()
    # Rclone stays in our group, so bounded C++ shutdown reaches descendants.
    os.environ['SOLAR_AUTO_PROCESS_GROUP']=str(os.getpid())
    c,_=load(path)
    try:os.nice(10)
    except OSError:pass
    with lock(c['sync']['lock_path']+'.auto') as guard:
        if guard is None:
            raise RuntimeError('automatic worker already running')
        periods={'sync':c['sync']['interval_sec'],
                 'cleanup':c['retention']['cleanup_interval_sec']}
        # Let DSP startup settle before the first maintenance pass.
        due={role:time.monotonic()+period for role,period in periods.items()}
        report(c,'INFO','AUTO MAINTENANCE START: sync_sec='+str(periods['sync'])+
               '; cleanup_sec='+str(periods['cleanup']))
        while True:
            for role in ('cleanup','sync'):
                if time.monotonic() < due[role]:continue
                try:run(role,path)
                except Exception as e:report(c,'ERROR','AUTO '+role.upper()+' FAILED: '+safe_error(e))
                # Delay after completion: no overlapping jobs or catch-up burst.
                due[role]=time.monotonic()+periods[role]
            time.sleep(min(.5,max(0,min(due.values())-time.monotonic())))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True)
    p.add_argument('--parent-pid',type=int,required=True);args=p.parse_args()
    try:serve(args.config,args.parent_pid)
    except StopWorker:report(None,'INFO','AUTO MAINTENANCE STOPPED; pending files remain for a later sync')
    except Exception as e:
        report(None,'ERROR','AUTO MAINTENANCE FAILED: '+safe_error(e));raise SystemExit(2)
