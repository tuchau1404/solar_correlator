#!/usr/bin/env python3
"""Version 1 canonical-copy tool. No fictitious historical versions supported."""
import argparse,difflib,json,pathlib
from solar_storage.config import load
p=argparse.ArgumentParser();p.add_argument('input');p.add_argument('output');a=p.parse_args()
src=pathlib.Path(a.input).resolve();dst=pathlib.Path(a.output).resolve()
if src==dst or dst.exists():raise SystemExit('Refusing to overwrite input/existing output')
c,_=load(src);new=json.dumps(c,indent=2)+'\n'
print(''.join(difflib.unified_diff(src.read_text().splitlines(True),new.splitlines(True),fromfile=str(src),tofile=str(dst))))
with dst.open('x') as f:f.write(new)
