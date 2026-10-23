#!/usr/bin/env python3
import argparse,json,sys
from solar_storage.config import load
p=argparse.ArgumentParser();p.add_argument('--config',required=True);a=p.parse_args()
try:
    c,h=load(a.config);print(json.dumps({'config':c,'config_hash':h},allow_nan=False))
except Exception as e:print(str(e),file=sys.stderr);sys.exit(2)
