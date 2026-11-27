"""方式Cの明示配置用factory。通常mainの既定providerは変更しない。"""
import os
import sys
from dataclasses import asdict

from app.submission_rpc import target_from, reject


def configured_snapshot_provider(db, actor, storage, basis_id, config, *, enabled=False):
    """固定設定のworkspaceとDB基準が一致するときだけ実UDSへ接続する。

    HTTPのbasis IDは認可・設定照合の索引にすぎない。パス・接続先・鍵・世代を
    クライアントから受け取らず、台帳も自動作成しない。Linux配置は別途承認。
    """
    if enabled is not True or sys.platform != 'linux' or os.geteuid() == 0:
        reject()
    from app import models
    from app.submissions import lock_target
    from app.work_environment import SnapshotEditingSessions
    from app.workspace_bootstrap import keys_from
    from app.submission_authorization import authorized_rpc_provider
    from app.submission_ledger import SQLiteReplayLedger
    from app.submission_uds import UnixSocketTransport
    from app.submission_git import GitSnapshotStore, SubmissionReviewGit
    basis = db.get(models.WorkBasis, basis_id, populate_existing=True)
    target = target_from(config['target'])
    if (basis is None or basis.state != 'ACTIVE' or basis.member_id != actor.id
            or basis.workspace_id != config['workspace_id']
            or basis.source_generation != config['generation']
            or lock_target(db, actor, basis.team_id, basis.project_id, basis.task_id) != target
            or (basis.team_id,basis.project_id,basis.task_id,basis.member_id,basis.folder_path) !=
               (target.team_id,target.project_id,target.task_id,target.student_id,target.folder_path)):
        reject()
    keys, auth = keys_from(config)
    return authorized_rpc_provider(db, actor, rpc_keys=keys, authorization_key=auth,
        collector_id=config['collector_id'],target=target,generation=basis.source_generation,
        store=GitSnapshotStore(SubmissionReviewGit(storage).session),sessions=SnapshotEditingSessions(db),
        transport=UnixSocketTransport(config['endpoint'],config['owner_uid'],enabled=True),
        ledger=SQLiteReplayLedger(config['client_ledger']))


def configured_snapshot_collector(config, *, enabled=False):
    """用途別鍵と固定workspaceに束縛した収集サーバー。DB・Gitは渡さない。"""
    if enabled is not True or sys.platform != 'linux' or os.geteuid() == 0:
        reject()
    from app.workspace_bootstrap import keys_from
    from app.submission_authorization import CollectorAuthorizationVerifier
    from app.submission_collector import configured_server
    target = target_from(config['target'])
    keys, auth = keys_from(config)
    verifier = CollectorAuthorizationVerifier(auth,config['collector_id'],target,config['generation'],
        workspace_id=config['workspace_id'])
    return configured_server(endpoint=config['endpoint'],client_uid=config['client_uid'],
        client_gid=config['client_gid'],ledger_path=config['server_ledger'],
        request_key_file=config['request_key_file'],response_key_file=config['response_key_file'],
        root=config['root'],target=target,owner_uid=config['owner_uid'],generation=config['generation'],
        authorize=verifier,enabled=True)


if __name__ == '__main__':
    import argparse,json,signal
    from app.workspace_bootstrap import private_bytes
    parser=argparse.ArgumentParser(description='別途承認後の合成収集サービス専用入口')
    parser.add_argument('--approved-synthetic',action='store_true')
    parser.add_argument('--settings',required=True)
    args=parser.parse_args()
    if not args.approved_synthetic or sys.platform!='linux' or os.geteuid()==0:reject()
    config=json.loads(private_bytes(args.settings,65536))
    server=configured_snapshot_collector(config,enabled=True)
    stop=[]
    for signum in (signal.SIGTERM,signal.SIGINT):
        signal.signal(signum,lambda *_:stop.append(True))
    server.serve(lambda:bool(stop))
