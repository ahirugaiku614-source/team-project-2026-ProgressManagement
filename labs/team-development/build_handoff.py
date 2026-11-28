"""Windowsから渡す成果物を明示したソース領域だけから作る。秘密値は出力しない。

Git履歴や一時領域を梱包しない。既存成果物を上書きせず、manifestは転送事故の
検出に使う。認証はSSHの既存ホスト鍵照合で行い、manifestを署名の代用にしない。
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


ROOT=Path(__file__).resolve().parents[2]
PREFIXES=('backend/app/','backend/migrations/','backend/tests/','frontend/src/','frontend/tests/','frontend/public/',
          'labs/team-development/','labs/code-server-submit/','docs/team-development-')
EXACT={'backend/Dockerfile','backend/requirements.txt','frontend/package.json','frontend/package-lock.json',
       'frontend/index.html','frontend/stage9-demo.html','frontend/vite.config.js','frontend/vite.demo.config.mjs',
       'docs/stage10c-handoff.md'}
EXACT.update({'backend/Dockerfile.stage10d','frontend/stage10d-native.html','frontend/vite.native.config.mjs',
              'docs/stage10d-handoff.md','backend/.dockerignore'})
SUFFIXES={'.py','.sql','.js','.mjs','.cjs','.vue','.json','.yaml','.yml','.template','.md','.html','.txt','.sh','.css','.svg','.png'}


def allowed(path):
    parts=path.split('/')
    return ((path in EXACT or path.startswith(PREFIXES))
            and not any(p in ('.git','node_modules','dist','artifacts','__pycache__') for p in parts)
            and not any(p.startswith('.env') for p in parts)
            and (Path(path).suffix in SUFFIXES or path in EXACT))


def build(output):
    output=Path(output).absolute()
    permitted=ROOT/'frontend'/'artifacts'
    if '..' in output.parts or not output.is_relative_to(permitted) or output.suffix!='.zip' or output.exists():
        raise ValueError('New artifact path required')
    if any(p.is_symlink() for p in [output,*output.parents]):raise ValueError('Unsafe output path')
    result=subprocess.run(['git','ls-files','--cached','--others','--exclude-standard','--',
        'backend','frontend','labs/team-development','labs/code-server-submit','docs'],cwd=ROOT,
        capture_output=True,text=True,check=True)
    paths=sorted({p for p in result.stdout.splitlines() if allowed(p)})
    files={};manifest={};blobs={}
    for name in paths:
        path=ROOT/name
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):raise ValueError('Unsafe source')
        data=path.read_bytes()
        # 実鍵・既知のトークン書式がある場合は値を表示せず梱包を止める。
        if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{25,}',data):
            raise ValueError('Secret candidate in '+name)
        files[name]=data;manifest[name]=hashlib.sha256(data).hexdigest()
        # Git属性による改行正規化も含めたblob IDを記録する。-wを使わずobjectを変更しない。
        blobs[name]=subprocess.run(['git','-c','core.autocrlf=true','hash-object','--path='+name,'--stdin'],cwd=ROOT,input=data,
            capture_output=True,check=True).stdout.decode().strip()
    head=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
    matches=all(subprocess.run(['git','rev-parse',head+':'+name],cwd=ROOT,capture_output=True,text=True).stdout.strip()==oid
                for name,oid in blobs.items())
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as stream,zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():archive.writestr(name,data)
        archive.writestr('stage10-manifest.json',json.dumps({'version':1,'files':manifest,
            'source':{'base_commit':head,'state':'COMMITTED' if matches else 'UNCOMMITTED',
                      'git_blobs':blobs}},sort_keys=True))
    return len(files)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:print(json.dumps({'status':'PACKAGED','files':build(args.output)}))
    except Exception:raise SystemExit('成果物作成を拒否しました。既存ファイル・秘密情報候補・パスを確認してください。') from None
