"""管理側の作業環境・編集セッション境界。既定では実接続を拒否する。

ブラウザーや利用者が変更できる拡張は認証局ではない。保存状態の証明は、
管理者が接続する独立したauthorityからだけ取得する。Linuxでの実証前に
環境変数やHTTP入力を根拠として検証器を有効化する機能は持たない。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
from uuid import uuid4

from app import models
from app.submissions import lock_target
from app.submission_authorization import VerifiedSession
from app.submission_provider import ProviderUnavailable
from app.task_review import TaskReviewError
from sqlalchemy.exc import IntegrityError


class DeniedAuthority:
    """実エディターの認証と全ウィンドウの保存状態は未接続。常に拒否する。"""
    def inspect(self, *args):
        raise ProviderUnavailable('Trusted editor authority is not connected')


@dataclass(frozen=True)
class EditorAttestation:
    session_id: str
    basis_id: str
    workspace_id: str
    source_generation: int
    challenge: str
    issued_at: int
    expires_at: int
    saved: bool
    exclusive_editor: bool


class EditingSessions:
    capture_policy = 'EDITOR_SAVED_V1'
    def __init__(self, db, authority=None, clock=time.time):
        self.db = db
        self.authority = authority or DeniedAuthority()
        self.clock = clock

    def verify(self, target, session_id, generation):
        row, basis = self.identity(target, session_id, generation)
        now = int(self.clock())
        challenge = str(uuid4())
        proof = self.authority.inspect(row.id, basis, challenge)
        now = int(self.clock())
        if (not isinstance(proof, EditorAttestation)
                or (proof.session_id, proof.basis_id, proof.workspace_id, proof.source_generation, proof.challenge)
                != (row.id, basis.id, basis.workspace_id, generation, challenge)
                or type(proof.source_generation) is not int
                or type(proof.issued_at) is not int or type(proof.expires_at) is not int
                or not proof.issued_at <= now < proof.expires_at <= proof.issued_at + 30
                or row.expires_at <= datetime.utcfromtimestamp(now)
                or proof.exclusive_editor is not True or type(proof.saved) is not bool):
            raise ProviderUnavailable('Editing attestation unavailable')
        return VerifiedSession(target, row.id, generation, min(proof.expires_at,
            int(row.expires_at.replace(tzinfo=timezone.utc).timestamp())), proof.saved)

    def identity(self, target, session_id, generation):
        """両方式で同じ本人・対象・世代・期限・失効を確認する。dirtyとは別の契約。"""
        row = self.db.get(models.WorkEditingSession, session_id, populate_existing=True)
        now = int(self.clock())
        if (row is None or row.capture_policy != self.capture_policy
                or row.revoked_at is not None or row.expires_at <= datetime.utcfromtimestamp(now)):
            raise ProviderUnavailable('Editing session unavailable')
        basis = self.db.get(models.WorkBasis, row.basis_id, populate_existing=True)
        actor = self.db.get(models.User, row.member_id)
        if basis is None or actor is None or basis.state != 'ACTIVE' or basis.member_id != row.member_id:
            raise ProviderUnavailable('Editing basis unavailable')
        current = lock_target(self.db, actor, basis.team_id, basis.project_id, basis.task_id)
        if basis.state != 'ACTIVE':raise ProviderUnavailable('Editing basis revoked')
        revision = self.db.get(models.WorkWorkspaceRevision, basis.workspace_id, populate_existing=True)
        if current != target or current.folder_path != basis.folder_path or basis.source_generation != generation or revision is None or revision.generation != generation:
            raise ProviderUnavailable('Editing binding unavailable')
        return row, basis

    def open(self, actor, basis):
        target = lock_target(self.db, actor, basis.team_id, basis.project_id, basis.task_id)
        if basis.member_id != actor.id or basis.state != 'ACTIVE':raise TaskReviewError('WORK_BASIS_INACTIVE')
        row = models.WorkEditingSession(id=str(uuid4()), basis_id=basis.id, member_id=actor.id,
            capture_policy=self.capture_policy,
            expires_at=datetime.utcfromtimestamp(self.clock()) + timedelta(minutes=10))
        self.db.add(row)
        self.db.flush()
        try:
            self.verify(target, row.id, basis.source_generation)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise ProviderUnavailable('Editing session could not be verified') from None
        return {'session_id': row.id, 'expires_at': row.expires_at}


class SnapshotEditingSessions(EditingSessions):
    """方式C：管理DBで発行した編集セッションの本人・権限・失効を検証する。

    未保存バッファの不存在は検証せず、saved=Trueも作らない。発行APIは既存の
    Firebase認証と作業基準の認可を通る。session IDだけでは他の対象へ転用できない。
    """
    capture_policy = 'SAVED_SNAPSHOT_V1'

    def verify(self, target, session_id, generation):
        from app.submission_authorization import SnapshotSession
        row, basis = self.identity(target, session_id, generation)
        return SnapshotSession(target, row.id, generation,
            int(row.expires_at.replace(tzinfo=timezone.utc).timestamp()),basis.id,basis.workspace_id)

    def open(self, actor, basis):
        result = super().open(actor, basis)
        return {**result, 'capture_policy': self.capture_policy}


class WorkEnvironment:
    """既存サービスへ準備・新世代取得を接続する。実rootやURLはHTTPから受けない。"""
    def __init__(self, service, sessions=None, *, editor_gateway=False):
        self.service = service
        self.sessions = sessions or EditingSessions(service.db)
        self.editor_gateway = editor_gateway

    def status(self, actor, team, project, task):
        lock_target(self.service.db, actor, team, project, task)
        # statusは初期化・失効・セッション作成を実行しない。
        connected = isinstance(self.sessions, SnapshotEditingSessions) or not isinstance(self.sessions.authority, DeniedAuthority)
        from app.task_review import DeniedWorkspaces
        ready = not isinstance(self.service.workspaces, DeniedWorkspaces)
        return {'state': 'CONFIGURED' if ready else 'UNAVAILABLE', 'launch_enabled': ready and connected and self.editor_gateway,
                'prepare_enabled': ready,
                'session_verification_available': connected, 'editor_url': None,
                'capture_policy': self.sessions.capture_policy,
                'reason': None if ready and connected else 'EDIT_SESSION_UNVERIFIED'}

    def prepare(self, actor, team, project, task, request_id, previous=None, session_id=None):
        db = self.service.db
        lock_target(db, actor, team, project, task)
        existing = db.query(models.WorkEnvironmentOperation).filter_by(member_id=actor.id, request_id=request_id).first()
        if existing:
            if (existing.team_id, existing.project_id, existing.task_id, existing.previous_basis_id, existing.editing_session_id) != (team, project, task, previous, session_id):
                raise TaskReviewError('ENVIRONMENT_REQUEST_CONFLICT')
            return self.operation_public(existing)
        if not self.status(actor, team, project, task)['prepare_enabled']:
            raise ProviderUnavailable('Environment is not connected')
        if bool(previous) != bool(session_id):raise TaskReviewError('ENVIRONMENT_REQUEST_CONFLICT')
        if db.query(models.WorkEnvironmentOperation).filter_by(member_id=actor.id,
                project_id=project, task_id=task, state='PROCESSING').first():
            raise TaskReviewError('ENVIRONMENT_OUTCOME_UNKNOWN')
        if previous is None and db.query(models.WorkBasis).filter_by(member_id=actor.id,
                project_id=project, task_id=task, state='ACTIVE').first():
            raise TaskReviewError('WORK_BASIS_INACTIVE')
        old = None
        if previous:
            old = db.get(models.WorkBasis, previous, populate_existing=True)
            if old is None or (old.member_id, old.team_id, old.project_id, old.task_id) != (actor.id, team, project, task):
                raise TaskReviewError('WORK_BASIS_NOT_FOUND')
            target = lock_target(db, actor, team, project, task)
            session = db.get(models.WorkEditingSession, session_id, populate_existing=True)
            if session is None or session.basis_id != old.id:
                raise TaskReviewError('EDIT_SESSION_UNVERIFIED')
            saved = self.sessions.verify(target, session_id, old.source_generation)
            # 保存済み未提出変更も、管理側の全体比較が一致しない限り更新を拒否する。
            # 最新版取得は方式Cでも別問題。未保存変更を保護できない間は拒否する。
            if isinstance(saved, VerifiedSession) and saved.saved is not True:raise TaskReviewError('UNSAVED_CHANGES')
            check = getattr(self.service.workspaces, 'unchanged', None)
            if check is None or check(old) is not True:raise TaskReviewError('UNSUBMITTED_CHANGES')
        operation = models.WorkEnvironmentOperation(id=str(uuid4()), request_id=request_id,
            member_id=actor.id, team_id=team, project_id=project, task_id=task,
            previous_basis_id=previous, editing_session_id=session_id, state='PROCESSING')
        db.add(operation)
        try:db.commit()
        except IntegrityError:
            db.rollback()
            raise TaskReviewError('ENVIRONMENT_REQUEST_CONFLICT') from None
        operation_id = operation.id
        try:
            def bind_initialization(basis_id,base):
                # RPC前にrequest・基準・basis IDを永続結合する。応答を失っても
                # 読取り照会で同じ受付を特定でき、別の初期化を推測で開始しない。
                changed=db.query(models.WorkEnvironmentOperation).filter_by(id=operation_id,
                    state='PROCESSING',result_basis_id=None).update(
                        {'result_basis_id':basis_id,'base_commit':base},synchronize_session=False)
                if changed!=1:raise ProviderUnavailable('Initialization admission changed')
                db.commit()
            basis = self.service.start(actor, team, project, task,
                initialization_request_id=request_id,before_initialize=bind_initialization)
            # 方式Cは旧ウィンドウの未保存状態を保証しない。新世代へ切り替えても
            # 旧環境・旧基準・セッションを保持し、開いたままの作業を失効させない。
            if old is not None and not isinstance(self.sessions,SnapshotEditingSessions):
                self.service.end(actor, old.id, 'SUPERSEDED')
            operation = db.get(models.WorkEnvironmentOperation, operation_id, populate_existing=True)
            operation.result_basis_id = basis.id
            operation.state = 'CONFIRMED'
            db.commit()
        except Exception:
            # 初期化が実行された可能性を隠さない。新ID発行・自動再実行はしない。
            db.rollback()
            raise ProviderUnavailable('Environment preparation outcome unknown') from None
        return self.operation_public(operation)

    @staticmethod
    def operation_public(row):
        return {'request_id': row.request_id, 'state': row.state, 'basis_id': row.result_basis_id,
                'outcome': 'RECORDED', 'previous_basis_id': row.previous_basis_id}

    def inspect_operation(self,actor,row):
        target=lock_target(self.service.db,actor,row.team_id,row.project_id,row.task_id)
        if row.member_id!=actor.id:raise ProviderUnavailable('Environment lookup denied')
        result=self.operation_public(row)
        inspect=getattr(self.service.workspaces,'lookup_operation',None)
        if inspect is not None and row.result_basis_id and row.base_commit:
            # 読取り照会だけを行う。証拠がCOMPLETEでもDB確認済みへ自動遷移しない。
            evidence=inspect(row,target)
            result['initialization_state']=evidence.get('state','UNKNOWN')
        else:result['initialization_state']='UNKNOWN'
        return result
