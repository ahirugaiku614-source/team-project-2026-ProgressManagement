"""承認後のrootless namespace内で、新規空領域だけの所有者・権限を準備する。

サービス自体は非root。既存volume・既存領域を修復する機能は持たない。
rootful Dockerや共有されたmapと異なる環境では拒否する。
"""
import argparse
import json
import os
from pathlib import Path
import stat
import sys


def prepare(root,ids):
    if sys.platform!='linux' or os.geteuid()!=0:raise ValueError('Approved rootless setup namespace required')
    expected=[(0,1000,1),(1,100000,65536)]
    for name in ('uid_map','gid_map'):
        rows=[tuple(map(int,line.split())) for line in Path('/proc/self/'+name).read_text().splitlines()]
        if rows!=expected:raise ValueError('Rootless mapping differs')
    if set(ids)!={'a','b','manager','gateway','db'} or len(set(ids.values()))!=5 or any(type(v) is not int or not 0<v<=65536 for v in ids.values()):
        raise ValueError('Distinct namespace identities required')
    root=Path(root)
    if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in [root,*root.parents]):raise ValueError('Unsafe setup root')
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o700 or any(root.iterdir()):
        raise ValueError('New empty namespace-root-owned directory required')
    entries={}
    def directory(relative,uid,mode):
        path=root/relative
        path.mkdir(mode=0o700)
        # 所有者変更後のFOWNERを不要にするため、chmodを先に実行する。
        path.chmod(mode);entries[relative]={'uid':uid,'gid':uid,'mode':oct(mode)}
    for member in ('a','b'):
        directory(member,0,0o711)
        for child in ('root','config','keys','collector-config','collector-keys','collector-ledger','editor-config','editor-data'):
            directory(member+'/'+child,ids[member],0o700)
        for child in ('socket','collector-socket'):
            directory(member+'/'+child,ids[member],0o770)
        directory(member+'/root/private',ids[member],0o700)
    for role,children in {'manager':('config','keys','client-ledgers','git'),
                          'gateway':('config','tls'),'db':('data','secret')}.items():
        directory(role,0,0o711)
        for child in children:directory(role+'/'+child,ids[role],0o700)
    for member in ('a','b'):
        directory('manager/client-ledgers/'+member,ids['manager'],0o700)
    # 全階層を作ってから深い順に変更する。0700の親を先に別UIDへ移して
    # 準備処理自身が到達不能になることを避け、CAP_DAC_OVERRIDEを要求しない。
    for relative,item in sorted(entries.items(),key=lambda p:p[0].count('/'),reverse=True):
        os.chown(root/relative,item['uid'],item['gid'])
    with (root/'layout.json').open('x',encoding='utf-8') as stream:
        json.dump({'version':1,'entries':entries},stream,sort_keys=True)
    (root/'layout.json').chmod(0o600)
    root.chmod(0o711)
    return len(entries)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--approved-synthetic',action='store_true');parser.add_argument('--root',required=True)
    for role in ('a','b','manager','gateway','db'):parser.add_argument('--'+role,required=True,type=int)
    args=parser.parse_args()
    if not args.approved_synthetic:raise SystemExit('別途承認が必要です')
    try:print(json.dumps({'status':'PREPARED','directories':prepare(args.root,{r:getattr(args,r) for r in ('a','b','manager','gateway','db')})}))
    except Exception:raise SystemExit('新規領域の準備を拒否しました。既存領域は再利用しません。') from None
