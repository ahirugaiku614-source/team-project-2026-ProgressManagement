"""ZIPの自己申告OIDではなく、管理者が信頼するローカルGit commitと全対象blobを照合する。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


def verify(archive,repository,commit):
    if not re.fullmatch('[0-9a-f]{40}',commit):raise ValueError('Full trusted commit required')
    def git(*args,data=None):
        # Windowsで梱包したCRLFもLinuxで同じblobへ正規化する。実ファイルは変更しない。
        return subprocess.run(['git','-C',str(repository),'-c','core.autocrlf=true',*args],input=data,capture_output=True,check=True).stdout.strip().decode()
    if git('rev-parse',commit+'^{commit}')!=commit:raise ValueError('Commit unavailable')
    with zipfile.ZipFile(archive) as source:
        manifest=json.loads(source.read('stage10-manifest.json'))
        files=manifest['files'];declared=manifest['source']['git_blobs']
        if set(files)!=set(declared) or set(source.namelist())!=set(files)|{'stage10-manifest.json'}:raise ValueError('Manifest mismatch')
        for name,digest in files.items():
            # ファイル名でGit optionやtree指定を注入させない。
            from build_handoff import allowed
            if not allowed(name) or '..' in Path(name).parts or ':' in name or name.startswith('/') or '\\' in name:raise ValueError('Invalid source path')
            data=source.read(name)
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Archive mismatch')
            blob=git('hash-object','--path='+name,'--stdin',data=data)
            if blob!=declared[name] or git('rev-parse',commit+':'+name)!=blob:raise ValueError('Trusted commit mismatch')
    return len(files)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--archive',required=True);parser.add_argument('--repository',required=True);parser.add_argument('--trusted-commit',required=True)
    args=parser.parse_args()
    try:print(json.dumps({'status':'COMMIT_MATCH','files':verify(args.archive,args.repository,args.trusted_commit)}))
    except Exception:raise SystemExit('信頼済みcommitとの照合に失敗しました。未commitの成果物は確定版ではありません。') from None
