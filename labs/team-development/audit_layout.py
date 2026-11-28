"""既存layoutのメタデータだけを読み取る。修復・削除・権限変更を行わない。"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys


def audit(root):
    if sys.platform!='linux' or os.geteuid()!=0:raise ValueError('Approved rootless setup namespace required')
    expected=[(0,1000,1),(1,100000,65536)]
    for name in ('uid_map','gid_map'):
        if [tuple(map(int,line.split())) for line in Path('/proc/self/'+name).read_text().splitlines()]!=expected:raise ValueError('Unexpected rootless map')
    root=Path(root)
    if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in [root,*root.parents]):raise ValueError('Unsafe layout')
    marker=root/'layout.json';info=marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600:raise ValueError('Unsafe layout marker')
    entries=json.loads(marker.read_text())
    if entries['version']!=1 or len(entries['entries'])!=37:raise ValueError('Unexpected layout definition')
    for relative,item in entries['entries'].items():
        if relative.startswith('/') or '..' in Path(relative).parts or '\\' in relative:raise ValueError('Invalid entry')
        path=root/relative
        if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('Linked layout')
        actual=path.lstat()
        if not stat.S_ISDIR(actual.st_mode) or (actual.st_uid,actual.st_gid,stat.S_IMODE(actual.st_mode))!=(item['uid'],item['gid'],int(item['mode'],8)):raise ValueError('Layout metadata differs')
    # 空であるとは宣言しない。DB data・鍵・台帳の内容と用途は別途照合する。
    return {'status':'METADATA_MATCH_ONLY','directories':37,'automatic_reuse':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
    try:print(json.dumps(audit(args.root)))
    except Exception:raise SystemExit('既存layoutの再利用を保留してください。修復せず、新しい専用領域を準備してください。') from None
