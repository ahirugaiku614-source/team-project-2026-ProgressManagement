"""固定設定から初期化RPC・管理側アダプター・socket gatewayを構築する。

通常mainから自動接続しない。実editor authorityは未成立なので、bootstrapでも
セッション検証や提出を有効化しない。HTTP入力・任意モジュール名を設定に使わない。
"""
import json
import os
import stat
import sys

from app.submission_ledger import private_storage
from app.submission_rpc import RPCKeys,target_from,reject


def private_bytes(path,limit):
    identity=private_storage(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if (info.st_dev,info.st_ino)!=identity:reject()
        data=os.read(fd,limit+1)
        if len(data)>limit:reject()
        return data
    finally:os.close(fd)


def keys_from(settings):
    keys=[private_bytes(settings[name],64) for name in ('request_key_file','response_key_file','authorization_key_file')]
    if any(not 32<=len(key)<=64 for key in keys) or len(set(keys))!=3:reject()
    return RPCKeys(*keys[:2]),keys[2]


def configured_workspace_server(settings,*,enabled=False):
    # 非Linux・未承認設定ならファイルを読む前に拒否する。
    if enabled is not True or sys.platform!='linux' or os.geteuid()==0:reject()
    from app.workspace_rpc import WorkspaceRPCServer,WorkspaceAuthorizationVerifier,MAX_FRAME
    from app.workspace_journal import WorkspaceJournal
    from app.submission_server import UDSServer
    from app.workspace_files import LinuxWorkspaces
    target=target_from(settings['target'])
    LinuxWorkspaces(settings['root'],settings['owner_uid'],settings['owner_gid'],
        member_id=target.student_id,project_id=target.project_id)
    keys,auth=keys_from(settings)
    journal=WorkspaceJournal(settings['journal'])  # 欠落を自動provisionしない。
    handler=WorkspaceRPCServer(keys,target,settings['root'],settings['owner_uid'],settings['owner_gid'],
        journal,WorkspaceAuthorizationVerifier(auth,target))
    return UDSServer(settings['endpoint'],handler,settings['client_uid'],settings['client_gid'],
                     enabled=True,timeout=10,max_request=MAX_FRAME)


def build_backend_environment(db,actor,storage,settings,*,enabled=False):
    if enabled is not True or sys.platform!='linux':reject()
    from app.workspace_rpc import RemoteWorkspaces,WorkspaceAuthorizationIssuer,MAX_FRAME
    from app.submission_uds import UnixSocketTransport
    from app.workspace_files import MappedWorkspaces
    from app.task_review import TaskReviewService
    from app.work_environment import WorkEnvironment,SnapshotEditingSessions
    adapters={}
    for config in settings:
        target=target_from(config['target'])
        if target.student_id!=actor.id:continue
        keys,auth=keys_from(config)
        client=RemoteWorkspaces(keys,UnixSocketTransport(config['endpoint'],config['owner_uid'],enabled=True,max_request=MAX_FRAME),
            WorkspaceAuthorizationIssuer(db,actor,auth),member_id=target.student_id,project_id=target.project_id)
        # workspace証拠と方式CのDBセッションを照合する。dirty値は作らない。
        def verify_capture(basis,row,fixed=target):
            from app.submission_authorization import SnapshotSession
            evidence=SnapshotEditingSessions(db).verify(fixed,row.editing_session_id,basis.source_generation)
            return (type(evidence) is SnapshotSession and row.student_id==actor.id
                    and (evidence.basis_id,evidence.workspace_id)==(basis.id,basis.workspace_id))
        client.collection_verifier=verify_capture
        if (actor.id,target.project_id) in adapters:reject()
        adapters[(actor.id,target.project_id)]=client
    service=TaskReviewService(db,storage,workspaces=MappedWorkspaces(adapters))
    sessions=SnapshotEditingSessions(db)
    # 方式CのDBセッションを使用する。収集providerとgatewayの実接続は別途承認。
    return WorkEnvironment(service,sessions,editor_gateway=False)


def install_backend(app,member_resolver,settings,*,enabled=False,collectors=(),gateway_enabled=False):
    """起動管理者が固定設定で呼ぶ。認証・DB依存は既存関数をそのまま使う。"""
    if enabled is not True:reject()
    from fastapi import Depends
    from app.db.database import get_db
    from app.project_storage import get_project_storage
    from app.task_review_api import get_work_environment,get_task_review_service,get_review_sessions,get_gateway_environment
    if type(gateway_enabled) is not bool:reject()
    def runtime(actor=Depends(member_resolver),db=Depends(get_db),storage=Depends(get_project_storage)):
        result=build_backend_environment(db,actor,storage,settings,enabled=True)
        result.editor_gateway=gateway_enabled
        return result
    def service(current=Depends(runtime)):return current.service
    def sessions(current=Depends(runtime)):return current.sessions
    app.dependency_overrides[get_work_environment]=runtime
    app.dependency_overrides[get_task_review_service]=service
    app.dependency_overrides[get_review_sessions]=sessions
    # cookie gateにはTeam付きFirebase依存を要求しない。既存capability検証が
    # actor・workspace・sessionをDBから特定する。通常APIの認証は変更しない。
    def gateway(db=Depends(get_db)):
        from app.task_review import TaskReviewService
        from app.work_environment import WorkEnvironment,SnapshotEditingSessions
        # gateはDBのcapabilityと現権限だけを読み、Git・初期化先には接続しない。
        return WorkEnvironment(TaskReviewService(db),SnapshotEditingSessions(db),editor_gateway=gateway_enabled)
    app.dependency_overrides[get_gateway_environment]=gateway
    if collectors:
        from fastapi import Request
        from app.snapshot_runtime import configured_snapshot_provider
        from app.submission_provider import get_submission_provider
        def provider(request:Request,actor=Depends(member_resolver),db=Depends(get_db),storage=Depends(get_project_storage)):
            from app import models
            basis=db.get(models.WorkBasis,str(request.path_params.get('basis_id','')),populate_existing=True)
            if basis is None or basis.member_id!=actor.id:reject()
            matches=[c for c in collectors if c['workspace_id']==basis.workspace_id]
            if len(matches)!=1:reject()
            return configured_snapshot_provider(db,actor,storage,basis.id,matches[0],enabled=True)
        app.dependency_overrides[get_submission_provider]=provider


def socket_gateway_main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--settings',required=True)
    parser.add_argument('--approved-isolated',action='store_true')
    args=parser.parse_args()
    if not args.approved_isolated or sys.platform!='linux' or os.geteuid()==0:reject()
    from app.editor_socket_gateway import SocketGateway,FixedLeaseCheck,create_app
    import uvicorn
    config=json.loads(private_bytes(args.settings,65536))
    gateway=SocketGateway(config['targets'],config['origin'],FixedLeaseCheck(config['manager']),
        enabled=True,upstream_cookie_names=config.get('upstream_cookie_names',[]))
    uvicorn.run(create_app(gateway),host='0.0.0.0',port=8090,access_log=False,log_level='warning',
                limit_concurrency=32,ws_max_size=1024*1024,ws_max_queue=8)


if __name__=='__main__':
    import argparse,signal
    parser=argparse.ArgumentParser();parser.add_argument('--settings',required=True)
    parser.add_argument('--approved-isolated',action='store_true');args=parser.parse_args()
    if not args.approved_isolated:reject()
    settings=json.loads(private_bytes(args.settings,65536))
    server=configured_workspace_server(settings,enabled=True)
    stopped=[]
    for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,lambda *_:stopped.append(True))
    server.serve(lambda:bool(stopped))
