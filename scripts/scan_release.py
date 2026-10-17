"""Scan tracked/staged/history text. Private denylist may be supplied by env.

Never print matched data. Additional local scanners/manual review remain needed.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
PATTERNS={
    'github_credential':re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})'),
    'private_key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'dpapi_blob':re.compile(r'01000000d08c9ddf[0-9a-f]{100,}',re.I),
    'personal_windows_path':re.compile(r'[A-Za-z]:[/\\]+Users[/\\]+[^\s<>"\']+',re.I),
    'realistic_cn_mobile':re.compile(r'(?<![A-Za-z0-9])(?:\+86)?1[3-9]\d{9}(?![A-Za-z0-9])'),
}
BLOCKED_SUFFIXES={'.png','.jpg','.jpeg','.webp','.sqlite','.sqlite3','.db','.exe','.dll','.apk','.lnk','.zip','.pyc','.ico'}
BLOCKED_NAMES={'config.json','tray-root.json','.env','paddle-models.manifest.json'}

def git(*args, binary=False):
    data=subprocess.check_output(['git',*args],cwd=ROOT)
    return data if binary else data.decode('utf-8')

def check(name,data,denylist):
    errors=[]
    path=Path(name)
    if path.suffix.lower() in BLOCKED_SUFFIXES or path.name in BLOCKED_NAMES or any(x in {'.venv','.venv-wechat','__pycache__','backup','downloads','paddle-models'} for x in path.parts):
        errors.append('forbidden_artifact')
    try: text=data.decode('utf-8-sig')
    except UnicodeDecodeError:return errors+['non_text_payload']
    for label,pattern in PATTERNS.items():
        if pattern.search(text):errors.append(label)
    if any(value and (value in text or json.dumps(value,ensure_ascii=True)[1:-1] in text) for value in denylist):errors.append('private_identifier')
    if re.search(r'"SecretProtected"\s*:\s*"[^"\s]+"',text):errors.append('populated_secret_field')
    return errors

def main():
    p=argparse.ArgumentParser();p.add_argument('--history',action='store_true');p.add_argument('--staged',action='store_true');args=p.parse_args()
    deny=[]
    if os.getenv('SMS_RELEASE_DENYLIST'):
        deny=json.loads(Path(os.environ['SMS_RELEASE_DENYLIST']).read_text(encoding='utf-8'))
    failures=[];checked=0
    paths=git('ls-files','-z').split('\0')
    for name in filter(None,paths):
        data=git('show',':'+name,binary=True) if args.staged else (ROOT/name).read_bytes()
        checked+=1
        for error in check(name,data,deny):failures.append({'file':name,'reason':error})
    if args.history:
        for commit in git('rev-list','--all').splitlines():
            for row in filter(None,git('ls-tree','-r','--name-only','-z',commit).split('\0')):
                checked+=1
                for error in check(row,git('show',commit+':'+row,binary=True),deny):failures.append({'file':row,'reason':error,'commit':commit[:12]})
    print(json.dumps({'passed':not failures,'checked_blobs':checked,'failures':failures},ensure_ascii=True))
    return bool(failures)

if __name__=='__main__':sys.exit(main())
