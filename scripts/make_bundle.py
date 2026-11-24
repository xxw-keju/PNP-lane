#!/usr/bin/env python3
"""Bundle only allowlisted source/docs/tests and a wheel; never package runtime state."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import zipfile


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--wheel',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    files=[]
    for name in ('pyproject.toml','MANIFEST.in','README.md','CHANGELOG.md','LICENSE','config.example.json','.gitignore'):
        files.append(root/name)
    for directory,extensions in [('src',{'.py','.html','.js','.css'}),('tests',{'.py'}),('scripts',{'.py','.sh'}),('docs',{'.md'})]:
        files.extend(p for p in (root/directory).rglob('*') if p.is_file() and p.suffix in extensions and
                     not any(part in ('__pycache__','state','jobs','catalog','assessments') or part.endswith('.egg-info') for part in p.relative_to(root).parts[:-1]))
    if not args.wheel.is_file() or args.wheel.suffix!='.whl':
        raise SystemExit('--wheel must name a built wheel')
    version=re.search(r'__version__ = "([0-9.]+)"',(root/'src/pnp_lane/__init__.py').read_text())[1]
    if not args.wheel.name.startswith('pnp_lane-'+version+'-'):
        raise SystemExit('Wheel version must match the source version')
    prefix='pnp-lane-'+version
    contents={str(Path(prefix)/f.relative_to(root)):f.read_bytes() for f in sorted(files)}
    contents[str(Path(prefix)/'wheels'/args.wheel.name)]=args.wheel.read_bytes()
    manifest={'version':version,'runtime_data_included':False,'files':{
        k:{'bytes':len(v),'sha256':hashlib.sha256(v).hexdigest()} for k,v in contents.items()}}
    contents[prefix+'/MANIFEST.json']=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
    args.output.mkdir(parents=True,exist_ok=True)
    dest=args.output/(prefix+'.zip')
    with zipfile.ZipFile(dest,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name,body in contents.items():
            info=zipfile.ZipInfo(name,date_time=(2026,11,24,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644<<16
            z.writestr(info,body)
    print(json.dumps({'archive':str(dest),'files':len(contents),'bytes':dest.stat().st_size,
                      'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()},ensure_ascii=False))


if __name__=='__main__':main()
