"""SSHで受け取った成果物を、新しい領域だけへ整合性確認後に展開する。"""
import argparse
import hashlib
import json
from pathlib import Path,PurePosixPath
import zipfile


def unpack(source,destination):
    destination=Path(destination).absolute()
    if destination.exists() or any(p.is_symlink() for p in [destination,*destination.parents]):
        raise ValueError('New destination required')
    with zipfile.ZipFile(source) as archive:
        infos=archive.infolist()
        if len(infos)>3000 or len({x.filename for x in infos})!=len(infos) or sum(x.file_size for x in infos)>100*1024*1024:
            raise ValueError('Invalid archive limits')
        for info in infos:
            path=PurePosixPath(info.filename)
            if (path.is_absolute() or str(path)!=info.filename
                    or any(p in ('..','.') or ':' in p for p in path.parts) or '\\' in info.filename):
                raise ValueError('Invalid entry')
        manifest=json.loads(archive.read('stage10-manifest.json'))
        if manifest.get('version')!=1 or set(manifest['files'])!={x.filename for x in infos}-{'stage10-manifest.json'}:
            raise ValueError('Manifest mismatch')
        files={}
        for name,digest in manifest['files'].items():
            data=archive.read(name)
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Content mismatch')
            files[name]=data
    # 全バイトの照合完了後に新規領域を作る。途中失敗も既存領域へ再展開しない。
    destination.mkdir(mode=0o700)
    for name,data in files.items():
        path=destination/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with path.open('xb') as stream:stream.write(data)
        # 梱包対象は公開可能な製品ソースだけ。非root imageから読める0644にする。
        # 鍵・台帳・実設定はarchiveに含めず、この権限設定の対象にしない。
        path.chmod(0o644)
    for path in destination.rglob('*'):
        if path.is_dir():path.chmod(0o755)
    return len(files)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--archive',required=True);parser.add_argument('--destination',required=True)
    args=parser.parse_args()
    try:print(json.dumps({'status':'VERIFIED','files':unpack(args.archive,args.destination)}))
    except Exception:raise SystemExit('新規展開を拒否しました。既存領域は上書きしません。') from None
