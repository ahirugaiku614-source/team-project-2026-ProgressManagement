"""rootless準備namespaceで新規設定だけを設置する。既存設定を修復・置換しない。"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys


def install(layout,bundle,phase):
    from audit_layout import audit
    audit(layout)
    root=Path(layout);source=Path(bundle)
    if phase not in ('prepare','connected'):raise ValueError('Invalid deployment phase')
    if any(p.is_symlink() for p in [source,*source.parents]) or not source.is_dir():raise ValueError('Unsafe bundle')
    entries=json.loads((root/'layout.json').read_text())['entries']
    suffix='stage10d-'+phase
    mapping={'backend.json':('manager/config/'+suffix,'backend.json'),
        'a-workspace.json':('a/config/'+suffix,'settings.json'),'b-workspace.json':('b/config/'+suffix,'settings.json'),
        'nginx.conf':('gateway/config/'+suffix,'nginx.conf')}
    if phase=='connected':
        mapping.update({'a-collector.json':('a/collector-config/'+suffix,'settings.json'),
            'b-collector.json':('b/collector-config/'+suffix,'settings.json'),
            'socket-gateway.json':('gateway/config/'+suffix,'socket-gateway.json'),
            'nginx.conf':('gateway/config/'+suffix,'nginx.conf'),
            'a-editor.yaml':('a/editor-config/'+suffix,'config.yaml'),
            'b-editor.yaml':('b/editor-config/'+suffix,'config.yaml')})
    destinations={parent for parent,_ in mapping.values()}
    if any((root/path).exists() for path in destinations):raise ValueError('Existing phase settings; use a new layout')
    for name,(parent,_) in mapping.items():
        item=(source/name).lstat()
        if not stat.S_ISREG(item.st_mode) or item.st_nlink!=1 or item.st_mode&0o077:raise ValueError('Private ordinary configuration required')
    # 全入力を確認してから作成。途中失敗時は残存を証拠として保持し、再実行しない。
    for parent in destinations:(root/parent).mkdir(mode=0o700)
    for name,(parent,filename) in mapping.items():
        data=(source/name).read_bytes();path=root/parent/filename
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        owner=entries[str(Path(parent).parent).replace('\\','/')]
        os.chown(path,owner['uid'],owner['gid'])
    for parent in destinations:
        owner=entries[str(Path(parent).parent).replace('\\','/')]
        os.chown(root/parent,owner['uid'],owner['gid'])
    return len(mapping)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--layout',required=True);parser.add_argument('--bundle',required=True);parser.add_argument('--phase',choices=['prepare','connected'],required=True)
    args=parser.parse_args()
    try:print(json.dumps({'status':'NEW_CONFIGS_INSTALLED','files':install(args.layout,args.bundle,args.phase)}))
    except Exception:raise SystemExit('設定設置を停止しました。既存領域・途中の証拠は上書きしないでください。') from None
