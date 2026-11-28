"""管理者設定の固定rootと収集処理を接続する明示bootstrap。自動起動はしない。"""
from contextlib import ExitStack
import os
from pathlib import PurePosixPath
import stat
from types import SimpleNamespace
from app.submission_collection import CollectionOps, collect_task
from app.submission_folders import ManagedTaskRoot
from app.submission_rpc import CollectorRPCServer, RPCKeys, reject
from app.submission_ledger import SQLiteReplayLedger, private_storage
from app.submission_server import UDSServer
from app.managed_process import ManagedProcess
from dataclasses import asdict


class FixedRootCollector:
    def __init__(self, root, target, owner_uid):
        path=PurePosixPath(root)
        if not path.is_absolute() or str(path)!=root or '..' in path.parts or type(owner_uid) is not int or owner_uid<=0:reject()
        self.path,self.target,self.owner_uid=path,target,owner_uid

    def __call__(self,target,session):
        # RPC登録対象との一致を再確認。パスは要求から受け取らず管理設定を使う。
        if target!=self.target:reject()
        ops=CollectionOps()
        with ExitStack() as stack:
            fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
            stack.callback(os.close,fd)
            for part in self.path.parts[1:]:
                fd=ops.open_directory(fd,part);stack.callback(ops.close,fd)
                info=ops.info(fd)
                if info.st_uid not in (0,self.owner_uid) or info.st_mode & 0o022:reject()
            info=ops.info(fd)
            if info.st_uid!=self.owner_uid:reject()
            root=ManagedTaskRoot(target.team_id,target.project_id,target.student_id,fd,
                                 info.st_dev,info.st_ino,self.owner_uid)
            return collect_task(SimpleNamespace(target=target),root,ops)


class IsolatedRootCollector:
    """認可・永続受付の後だけ子を起動し、完成したフレームだけを親へ返す。"""
    def __init__(self, root, target, owner_uid, *, runner=None):
        fixed=FixedRootCollector(root,target,owner_uid)
        self.root,self.target,self.uid=str(fixed.path),target,owner_uid
        self.runner=runner or ManagedProcess(enabled=True)

    def __call__(self,target,session):
        from app.submission_rpc import decode_files
        if target!=self.target:reject()
        result=self.runner.run({'action':'COLLECT','root':self.root,'target':asdict(target),
            'owner_uid':self.uid,'parent_pid':os.getpid()})
        if not isinstance(result,dict) or set(result)!={'files'}:reject()
        return decode_files(result['files'])


def configured_server(*, endpoint, client_uid, client_gid, ledger_path, request_key_file,
                      response_key_file, root, target, owner_uid, generation,
                      authorize=None, enabled=False):
    """認可/編集セッション検証器なしでは拒否。キーは専用0600ファイルからだけ読む。

    初期台帳作成は管理者の別操作。ここでは既存台帳を検査し、欠落時に作らない。
    authorizeは署名だけでなく現在の認可とdirty状態の真正性を検証する信頼境界。
    """
    from app.submission_authorization import CollectorAuthorizationVerifier
    if (not isinstance(authorize,CollectorAuthorizationVerifier) or authorize.target!=target
            or authorize.generation!=generation or enabled is not True
            or type(generation) is not int or generation<=0):reject()
    keys=[]
    for path in (request_key_file,response_key_file):
        before=private_storage(path)
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            if (info.st_dev,info.st_ino)!=before or not stat.S_ISREG(info.st_mode):reject()
            key=os.read(fd,65)
            if not 32<=len(key)<=64:reject()
            keys.append(key)
        finally:os.close(fd)
    ledger=SQLiteReplayLedger(ledger_path)
    if authorize.key in keys:reject()  # 通信認証と学生認可で同じ鍵を使わない。
    source=IsolatedRootCollector(root,target,owner_uid)
    handler=CollectorRPCServer(RPCKeys(*keys),ledger,{target:(generation,source)},authorize)
    return UDSServer(endpoint,handler,client_uid,client_gid,enabled=True)
