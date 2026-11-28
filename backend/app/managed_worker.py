"""固定ワーカー入口。標準入出力以外の資格情報・FDを親から受け取らない。"""
from pathlib import Path
import sys

# -Iで起動し、管理側の固定コードだけをimportする。作業フォルダーをsys.pathへ入れない。
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import os
os.environ.clear()
import json
import stat
from types import SimpleNamespace
from app.submission_rpc import strict_json,target_from,encode_files,decode_files
from app.submission_collection import collect_task,CollectionOps
from app.submission_folders import ManagedTaskRoot
from app.worker_sandbox import restrict_filesystem


def mount_id(fd):
    with open('/proc/self/fdinfo/'+str(fd),encoding='ascii') as stream:
        values=[line.split(':',1)[1].strip() for line in stream if line.startswith('mnt_id:')]
    if len(values)!=1 or not values[0].isdigit():raise RuntimeError('Mount identity unavailable')
    return int(values[0])


class ReadonlyCollectionOps(CollectionOps):
    """同一FSのbind mountも拒否し、検査したFDそのものから本文を読む。"""
    def __init__(self,root):self.mount=mount_id(root)
    def info(self,fd):
        if mount_id(fd)!=self.mount:raise RuntimeError('Collection mount boundary changed')
        return super().info(fd)
    def read_file(self,directory,name,expected):
        from app.submission_collection import fingerprint
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        try:
            before=self.info(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or fingerprint(before)!=fingerprint(expected):raise RuntimeError('Collection identity changed')
            data=bytearray()
            while part:=os.read(fd,min(65536,1024*1024+1-len(data))):data.extend(part)
            if len(data)>1024*1024 or fingerprint(self.info(fd))!=fingerprint(before):raise RuntimeError('Collection changed')
            return bytes(data)
        finally:os.close(fd)


def execute(job):
    if sys.platform!='linux' or os.geteuid()==0:raise RuntimeError('Linux isolated worker required')
    from app.workspace_files import LinuxWorkspaces
    from app.workspace_scope import WorkspaceInitialization
    from app.workspace_rpc import decode_snapshot
    target=target_from(job['target']);root=job['root'];ops=CollectionOps();fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        from pathlib import PurePosixPath
        path=PurePosixPath(root)
        if not path.is_absolute() or str(path)!=root or '..' in path.parts:raise ValueError('Invalid root')
        for part in path.parts[1:]:
            child=ops.open_directory(fd,part);os.close(fd);fd=child
            info=os.fstat(fd)
            if info.st_uid not in (0,os.geteuid()) or info.st_mode & 0o022:raise ValueError('Unsafe source parent')
        info=os.fstat(fd)
        if info.st_uid!=os.geteuid() or job['owner_uid']!=os.geteuid():raise ValueError('Unexpected source owner')
        action=job['action']
        # 初期化コードは既存の安全な固定root実装を使う。必要なimportは制限前に完了する。
        adapter=None
        if action!='COLLECT':
            adapter=LinuxWorkspaces(root,job['owner_uid'],job['owner_gid'],member_id=target.student_id,project_id=target.project_id)
        metadata=os.open('/proc/self/fdinfo',os.O_RDONLY|os.O_DIRECTORY)
        try:restrict_filesystem(fd,writable=action=='INITIALIZE',expected_parent=job['parent_pid'],readonly_fds=(metadata,))
        finally:os.close(metadata)
        if action=='COLLECT':
            # Landlockだけではchmod等すべての操作を禁止できないため、収集元は
            # OSのread-only mountも必須とする。書込み可能な配置へfallbackしない。
            if not os.fstatvfs(fd).f_flag & os.ST_RDONLY:raise RuntimeError('Read-only collection mount required')
            bound=ManagedTaskRoot(target.team_id,target.project_id,target.student_id,fd,info.st_dev,info.st_ino,info.st_uid)
            return {'files':encode_files(collect_task(SimpleNamespace(target=target),bound,ReadonlyCollectionOps(fd)))}
        if action=='INITIALIZE':
            from dataclasses import asdict
            return {'proof':asdict(adapter.initialize_from_snapshot(target,job['base_commit'],job['basis_id'],decode_snapshot(job['files'])))}
        proof=WorkspaceInitialization(**{**job['proof'],'target':target})
        if action=='VERIFY':return {'verified':adapter.verify(proof)}
        if action=='UNCHANGED':
            basis=SimpleNamespace(id=proof.basis_id,workspace_id=proof.workspace_id,base_commit=proof.base_commit)
            return {'unchanged':adapter.verify(proof) and adapter.unchanged(basis)}
        raise ValueError('Unknown managed operation')
    finally:os.close(fd)


if __name__=='__main__':
    try:
        header=sys.stdin.buffer.read(4);size=int.from_bytes(header,'big')
        if len(header)!=4 or not 0<size<=15*1024*1024:raise ValueError('Invalid request')
        raw=sys.stdin.buffer.read(size)
        if len(raw)!=size or sys.stdin.buffer.read(1):raise ValueError('Invalid frame')
        result=execute(strict_json(raw));data=json.dumps(result,separators=(',',':')).encode()
        if len(data)>15*1024*1024:raise ValueError('Result exceeds limit')
        sys.stdout.buffer.write(len(data).to_bytes(4,'big')+data);sys.stdout.buffer.flush()
    except Exception:
        # 例外にパスや本文が含まれていても、子のstderr/応答へ出さない。
        raise SystemExit(2) from None
