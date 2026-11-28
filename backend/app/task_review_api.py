"""タスク限定収集を既存ChangeRequestへ接続する公開入口。

本文・OID・ref・絶対パスは入出力に含めない。依存注入はサーバー設定だけで行い、
HTTPパラメーターから模擬providerや模擬検証器を選択する機能は設けない。
"""
import time
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app import models
from app.db.database import get_db
from app.project_storage import get_project_storage, StorageConflict, StorageUnavailable
from app.review_git import ReviewInvalid, ReviewLimit
from app.submission_targets import TargetForbidden, TargetInvalid, TargetConflict
from app.submissions import lock_target, submit
from app.submission_api import SubmissionRequest
from app.submission_provider import get_submission_provider, ProviderUnavailable
from app.submission_authorization import DeniedSessionVerifier, VerifiedSession, check_session
from app.task_review import TaskReviewService, TaskReviewError, DeniedWorkspaces
from app.task_review_lifecycle import require_lifecycle


class PublicError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class SafeRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def safe(request):
            try:
                response = await original(request)
            except Exception as error:
                # 検証エラーに含まれる入力値やSQL/Gitの例外を画面へそのまま返さない。
                status, code = 503, 'REVIEW_SERVICE_UNAVAILABLE'
                if isinstance(error, PublicError):status, code = error.status, error.code
                elif isinstance(error, (RequestValidationError, ValidationError)):status, code = 422, 'INVALID_REQUEST'
                elif isinstance(error, TaskReviewError):status, code = 409, error.code
                elif isinstance(error, TargetForbidden):status, code = 403, 'REVIEW_FORBIDDEN'
                elif isinstance(error, TargetInvalid):status, code = 404, 'REVIEW_TARGET_NOT_FOUND'
                elif isinstance(error, (TargetConflict, StorageConflict, ReviewInvalid)):status, code = 409, 'REVIEW_EVIDENCE_CONFLICT'
                elif isinstance(error, ReviewLimit):status, code = 413, 'REVIEW_LIMIT_EXCEEDED'
                elif isinstance(error, HTTPException):
                    status = error.status_code
                    code = 'AUTHENTICATION_REQUIRED' if status == 401 else 'REVIEW_FORBIDDEN' if status == 403 else 'REVIEW_TARGET_NOT_FOUND' if status == 404 else 'REVIEW_REQUEST_REJECTED'
                response = JSONResponse(status_code=status, content={'detail': {'code': code}})
            response.headers['Cache-Control'] = 'no-store'
            return response
        return safe


def get_workspaces():
    return DeniedWorkspaces()


def get_review_sessions(db: Session = Depends(get_db)):
    from app.work_environment import SnapshotEditingSessions
    return SnapshotEditingSessions(db)


def get_task_review_service(db: Session = Depends(get_db), storage=Depends(get_project_storage),
                            workspaces=Depends(get_workspaces)):
    require_lifecycle(db)
    return TaskReviewService(db, storage, workspaces=workspaces)


def get_work_environment(service=Depends(get_task_review_service)):
    from app.work_environment import WorkEnvironment, SnapshotEditingSessions
    return WorkEnvironment(service, SnapshotEditingSessions(service.db))


def get_gateway_environment(runtime=Depends(get_work_environment)):
    """明示bootstrapではcookie gate用に認証依存を分離する。通常は既存runtime。"""
    return runtime


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    submission_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    deletion_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    previous_request_id: int | None = Field(default=None, gt=0)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SnapshotCollectionInput(EmptyInput):
    """方式Cはdirtyの自己申告を受け取らない。用途はDBセッションから判定する。"""
    editing_session_id: UUID
    request_id: UUID
    permit: str = Field(min_length=43, max_length=43, pattern=r'^[A-Za-z0-9_-]{43}$', repr=False)


class EnvironmentInput(EmptyInput):
    request_id: UUID
    previous_basis_id: UUID | None = None
    session_id: UUID | None = None


class EditorAccessInput(EmptyInput):
    session_id: UUID


class ConfirmedReview(ReviewInput):
    confirmation_id: UUID


class DeletionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    submission_id: UUID
    candidate_id: UUID
    confirmation_id: UUID


def arguments(payload):
    return dict(deletion_ids=[str(x) for x in payload.deletion_ids], previous_request_id=payload.previous_request_id)


def require_saved_session(service, sessions, actor, basis, source):
    """保存済み収集の証拠とは別に、現在の編集セッションを信頼済み検証器で確認する。"""
    target = lock_target(service.db, actor, basis.team_id, basis.project_id, basis.task_id)
    try:
        evidence = sessions.verify(target, source.editing_session_id, basis.source_generation)
        from app.submission_authorization import SnapshotSession
        if type(evidence) is SnapshotSession and (evidence.basis_id,evidence.workspace_id)!=(basis.id,basis.workspace_id):
            raise PublicError(403,'EDIT_SESSION_UNVERIFIED')
        if isinstance(evidence, VerifiedSession) and evidence.saved is False:
            raise PublicError(409, 'UNSAVED_CHANGES')
        # 同一対象・session・世代・有効期限も検査する。isDirtyのクライアント申告は使わない。
        class FixedEvidence:
            def verify(self, *args):return evidence
        check_session(FixedEvidence(), target, source.editing_session_id, basis.source_generation, int(time.time()))
        return evidence
    except PublicError:
        raise
    except Exception:
        raise PublicError(503, 'EDIT_SESSION_UNVERIFIED') from None


def create_task_review_router(member_resolver):
    router = APIRouter(route_class=SafeRoute)
    root = '/api/teams/{team_id}/projects/{project_id}/tasks/{task_id}/work-bases'

    @router.get('/api/teams/{team_id}/projects/{project_id}/change-requests/{request_id}/task-context')
    def review_task(team_id: int, project_id: int, request_id: int, user=Depends(member_resolver),
                    db: Session = Depends(get_db)):
        project = db.get(models.Project, project_id)
        change = db.get(models.ChangeRequest, request_id)
        if project is None or project.team_id != team_id or change is None or change.project_id != project_id:
            raise PublicError(404, 'REVIEW_TARGET_NOT_FOUND')
        conversion = db.query(models.TaskReviewConversion).filter_by(change_request_id=request_id).first()
        basis = db.get(models.WorkBasis, conversion.basis_id) if conversion else None
        task = db.get(models.Task, basis.task_id) if basis else None
        return {'task_id': basis.task_id if basis else None, 'task_title': task.title if task else None,
                'previous_request_id': conversion.previous_request_id if conversion else None}

    @router.get(root + '/environment')
    def environment(team_id: int, project_id: int, task_id: int, user=Depends(member_resolver),
                    runtime=Depends(get_work_environment)):
        return runtime.status(user, team_id, project_id, task_id)

    @router.post(root + '/environment')
    def prepare_environment(team_id: int, project_id: int, task_id: int, payload: EnvironmentInput,
                            user=Depends(member_resolver), runtime=Depends(get_work_environment)):
        return runtime.prepare(user, team_id, project_id, task_id, str(payload.request_id),
            str(payload.previous_basis_id) if payload.previous_basis_id else None,
            str(payload.session_id) if payload.session_id else None)

    @router.get(root + '/environment/requests/{request_id}')
    def environment_result(team_id: int, project_id: int, task_id: int, request_id: UUID,
                           user=Depends(member_resolver), runtime=Depends(get_work_environment)):
        db = runtime.service.db
        lock_target(db, user, team_id, project_id, task_id)
        row = db.query(models.WorkEnvironmentOperation).filter_by(member_id=user.id,
            team_id=team_id, project_id=project_id, task_id=task_id, request_id=str(request_id)).first()
        return runtime.inspect_operation(user,row) if row else {'outcome': 'UNKNOWN', 'state': 'UNKNOWN', 'request_id': str(request_id), 'basis_id': None}

    def scope(service, user, team, project, task, basis_id=None):
        require_lifecycle(service.db)
        lock_target(service.db, user, team, project, task)
        if basis_id is None:return None
        row = service.db.get(models.WorkBasis, str(basis_id), populate_existing=True)
        if row is None or (row.member_id, row.team_id, row.project_id, row.task_id) != (user.id, team, project, task):
            raise PublicError(404, 'WORK_BASIS_NOT_FOUND')
        return row

    def basis_public(row):
        return {'id': row.id, 'state': row.state, 'started_at': row.created_at,
                'base_version': row.base_commit, 'workspace_id': row.workspace_id,
                'source_generation': row.source_generation}

    def source_for(service, user, basis, sid):
        row = service.db.get(models.TaskSubmission, str(sid), populate_existing=True)
        if row is None or (row.student_id, row.team_id, row.project_id, row.task_id) != (user.id, basis.team_id, basis.project_id, basis.task_id):
            raise PublicError(404, 'COLLECTION_NOT_FOUND')
        return row

    @router.post(root, status_code=201)
    def start(team_id: int, project_id: int, task_id: int, payload: EmptyInput, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        scope(service, user, team_id, project_id, task_id)
        # 新しい実ファイル初期化アダプターは受付を先行記録するenvironment入口に
        # 限定する。旧入口からrequest IDなしで初期化を繰り返せないようにする。
        if getattr(service.workspaces, 'initialize_from_snapshot', None) is not None:
            raise PublicError(409, 'ENVIRONMENT_REQUEST_REQUIRED')
        # 旧入口から最新版取得の保存状態検査を迂回させない。
        if service.db.query(models.WorkBasis).filter_by(member_id=user.id, project_id=project_id,
                task_id=task_id, state='ACTIVE').first():raise PublicError(409, 'WORK_BASIS_INACTIVE')
        return basis_public(service.start(user, team_id, project_id, task_id))

    @router.get(root + '/current')
    def current(team_id: int, project_id: int, task_id: int, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        scope(service, user, team_id, project_id, task_id)
        rows = service.db.query(models.WorkBasis).filter_by(member_id=user.id, team_id=team_id, project_id=project_id, task_id=task_id).order_by(models.WorkBasis.created_at.desc()).all()
        return {'basis': basis_public(rows[0]) if rows else None}

    @router.get(root + '/{basis_id}')
    def basis(team_id: int, project_id: int, task_id: int, basis_id: UUID, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        return basis_public(scope(service, user, team_id, project_id, task_id, basis_id))

    @router.post(root + '/{basis_id}/editing-sessions', status_code=201)
    def editing_session(team_id: int, project_id: int, task_id: int, basis_id: UUID, payload: EmptyInput,
                        user=Depends(member_resolver), runtime=Depends(get_work_environment)):
        basis = scope(runtime.service, user, team_id, project_id, task_id, basis_id)
        return runtime.sessions.open(user, basis)

    @router.post(root + '/{basis_id}/editing-sessions/{session_id}/revoke')
    def revoke_session(team_id: int, project_id: int, task_id: int, basis_id: UUID, session_id: UUID,
                       payload: EmptyInput, user=Depends(member_resolver), runtime=Depends(get_work_environment)):
        basis = scope(runtime.service, user, team_id, project_id, task_id, basis_id)
        db = runtime.service.db
        row = db.get(models.WorkEditingSession, str(session_id), populate_existing=True)
        if row is None or row.basis_id != basis.id or row.member_id != user.id:raise PublicError(404, 'EDIT_SESSION_UNVERIFIED')
        if row.revoked_at is None:
            row.revoked_at = datetime.utcnow();db.commit()
        return {'session_id': row.id, 'state': 'REVOKED'}

    @router.post(root + '/{basis_id}/editor-access')
    def editor_access(team_id: int, project_id: int, task_id: int, basis_id: UUID,
                      payload: EditorAccessInput, response: Response, user=Depends(member_resolver),
                      runtime=Depends(get_work_environment)):
        import secrets
        from app.submissions import token_digest
        basis = scope(runtime.service, user, team_id, project_id, task_id, basis_id)
        if not runtime.editor_gateway:raise PublicError(503, 'EDITOR_GATEWAY_UNAVAILABLE')
        session = runtime.service.db.get(models.WorkEditingSession, str(payload.session_id))
        if session is None or session.basis_id != basis.id:raise PublicError(403, 'REVIEW_FORBIDDEN')
        target = lock_target(runtime.service.db, user, team_id, project_id, task_id)
        runtime.sessions.verify(target, session.id, basis.source_generation)
        token = secrets.token_urlsafe(32)
        from datetime import timedelta
        expires = min(session.expires_at, datetime.utcnow() + timedelta(seconds=60))
        runtime.service.db.add(models.WorkEditorAccess(token_digest=token_digest(token),
            session_id=session.id, workspace_id=basis.workspace_id, expires_at=expires))
        runtime.service.db.commit()
        path = '/code-server/' + basis.workspace_id + '/'
        # 平文capabilityはURL・応答JSONへ載せず、TLS前提のHttpOnly cookieだけに置く。
        response.set_cookie('work_editor_' + basis.workspace_id, token, secure=True, httponly=True,
            samesite='strict', path=path, max_age=60)
        return {'url': path, 'expires_at': expires}

    @router.get('/api/editor-gateway/{workspace_id}')
    def editor_gate(workspace_id: UUID, request: Request, runtime=Depends(get_gateway_environment)):
        import re
        from app.submissions import token_digest
        if not runtime.editor_gateway:raise PublicError(503, 'EDITOR_GATEWAY_UNAVAILABLE')
        token = request.cookies.get('work_editor_' + str(workspace_id), '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):raise PublicError(401, 'AUTHENTICATION_REQUIRED')
        db = runtime.service.db
        access = db.get(models.WorkEditorAccess, token_digest(token))
        if access is None or access.workspace_id != str(workspace_id) or access.expires_at <= datetime.utcnow():
            raise PublicError(401, 'AUTHENTICATION_REQUIRED')
        session = db.get(models.WorkEditingSession, access.session_id, populate_existing=True)
        basis = db.get(models.WorkBasis, session.basis_id) if session else None
        if basis is None or basis.workspace_id != str(workspace_id):raise PublicError(403, 'REVIEW_FORBIDDEN')
        actor = db.get(models.User, session.member_id)
        target = lock_target(db, actor, basis.team_id, basis.project_id, basis.task_id)
        runtime.sessions.verify(target, session.id, basis.source_generation)
        # auth_request専用の読取り。cookie更新・収集・復旧は行わない。
        return Response(status_code=204)

    @router.get(root + '/{basis_id}/deletion-candidates')
    def candidates(team_id: int, project_id: int, task_id: int, basis_id: UUID, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        scope(service, user, team_id, project_id, task_id, basis_id)
        # blob OIDやモードは管理側の削除証拠だけに保存し、HTTPへは出さない。
        return {'candidates': [{'id': k, 'path': v['path']} for k, v in service.deletion_candidates(user, str(basis_id)).items()]}

    @router.get(root + '/{basis_id}/requests/{request_id}')
    def request_result(team_id: int, project_id: int, task_id: int, basis_id: UUID, request_id: UUID,
                       user=Depends(member_resolver), service=Depends(get_task_review_service)):
        # 応答喪失で収集IDを受信できなくても、同じ本人・タスク・基準から照会できる。
        # 未登録は未実行の証明にしない。GETから復旧・収集・基準失効更新を呼ばない。
        row = scope(service, user, team_id, project_id, task_id, basis_id)
        source = service.db.query(models.TaskSubmission).filter_by(student_id=user.id,
            team_id=team_id, project_id=project_id, task_id=task_id, request_id=str(request_id)).first()
        if source is None:
            return {'outcome': 'UNKNOWN', 'request_id': str(request_id), 'submission_id': None}
        bound = service.db.get(models.WorkCaptureBinding, source.id)
        if bound is None or bound.basis_id != row.id:raise PublicError(409, 'COLLECTION_BASIS_CONFLICT')
        return {'outcome': 'RECORDED', 'request_id': str(request_id), 'submission_id': source.id,
                **result_public(service, row, source, service.db.get(models.TaskReviewConversion, source.id))}

    @router.get(root + '/{basis_id}/confirmations/{confirmation_id}')
    def confirmation(team_id: int, project_id: int, task_id: int, basis_id: UUID, confirmation_id: UUID,
                     user=Depends(member_resolver), service=Depends(get_task_review_service)):
        basis = scope(service, user, team_id, project_id, task_id, basis_id)
        preview = service.db.get(models.WorkReviewPreview, str(confirmation_id))
        if preview is None or preview.basis_id != basis.id:raise PublicError(404, 'CONFIRMATION_NOT_FOUND')
        source_for(service, user, basis, preview.submission_id)
        converted = service.db.get(models.TaskReviewConversion, preview.submission_id)
        state = ('INACTIVE' if basis.state != 'ACTIVE' else 'CONSUMED' if converted else
                 'EXPIRED' if preview.expires_at <= datetime.utcnow() else 'AVAILABLE')
        return {'confirmation_id': preview.id, 'submission_id': preview.submission_id,
                'state': state, 'expires_at': preview.expires_at}

    @router.get(root + '/{basis_id}/deletion-intents')
    def deletion_evidence(team_id: int, project_id: int, task_id: int, basis_id: UUID, submission_id: UUID,
                          user=Depends(member_resolver), service=Depends(get_task_review_service)):
        basis = scope(service, user, team_id, project_id, task_id, basis_id)
        source_for(service, user, basis, submission_id)
        bound = service.db.get(models.WorkCaptureBinding, str(submission_id))
        if bound is None or bound.basis_id != basis.id:raise PublicError(404, 'COLLECTION_NOT_FOUND')
        rows = service.db.query(models.TaskDeletionIntent).filter_by(basis_id=basis.id,
            submission_id=str(submission_id), member_id=user.id).all()
        return {'intents': [{'id': d.id, 'path': d.path} for d in rows]}

    @router.post(root + '/{basis_id}/deletion-intents', status_code=201)
    def deletion(team_id: int, project_id: int, task_id: int, basis_id: UUID, payload: DeletionInput, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        row = scope(service, user, team_id, project_id, task_id, basis_id)
        source_for(service, user, row, payload.submission_id)
        result = service.confirm_deletion(user, str(basis_id), str(payload.submission_id), str(payload.candidate_id), str(payload.confirmation_id))
        return {'deletion_id': result.id, 'path': result.path}

    @router.post(root + '/{basis_id}/collections', status_code=202)
    def collect(team_id: int, project_id: int, task_id: int, basis_id: UUID, payload: SubmissionRequest | SnapshotCollectionInput,
                user=Depends(member_resolver), service=Depends(get_task_review_service),
                sessions=Depends(get_review_sessions), provider=Depends(get_submission_provider)):
        row = scope(service, user, team_id, project_id, task_id, basis_id)
        with service.session_factory(row.project_id) as git:
            service._basis(git, user, row.id)
        # セッションは許可文字列とは独立して検証する。収集受付後、収集前に基準を固定する。
        from types import SimpleNamespace
        evidence = require_saved_session(service, sessions, user, row, SimpleNamespace(editing_session_id=str(payload.editing_session_id)))
        from app.submission_authorization import SnapshotSession
        if not isinstance(evidence, SnapshotSession):
            if isinstance(payload, SnapshotCollectionInput):raise PublicError(409, 'CAPTURE_POLICY_MISMATCH')
            if payload.has_unsaved_changes:raise PublicError(409, 'UNSAVED_CHANGES')
        existing = service.db.query(models.TaskSubmission).filter_by(student_id=user.id, task_id=task_id, request_id=str(payload.request_id)).first()
        if existing:
            binding = service.db.get(models.WorkCaptureBinding, existing.id)
            if binding is None or binding.basis_id != row.id:raise PublicError(409, 'COLLECTION_BASIS_CONFLICT')
        def failure(sid, error):
            from app.submission_collection import EmptyTaskCollection
            code = 'EMPTY_CAPTURE_UNSUPPORTED' if isinstance(error, EmptyTaskCollection) else 'COLLECTION_REVIEW_REQUIRED'
            service.db.add(models.WorkCollectionDiagnostic(submission_id=sid, code=code))
            service.db.commit()  # 本文・内部例外は記録せず固定分類だけを残す。
        def publish(sid):
            service.validate_collection_binding(user, row.id, sid)
            source = source_for(service, user, row, sid)
            require_saved_session(service, sessions, user, row, source)
        result, _ = submit(service.db, user, team_id, project_id, task_id, payload, provider,
                           before_collect=lambda sid: service.bind_collection(user, row.id, sid),
                           before_publish=publish, on_failure=failure)
        diagnostic = service.db.get(models.WorkCollectionDiagnostic, result['id'])
        return {'submission_id': result['id'], 'collection_state': result['state'], 'error_code': diagnostic.code if diagnostic else None}

    @router.post(root + '/{basis_id}/preview')
    def preview(team_id: int, project_id: int, task_id: int, basis_id: UUID, payload: ReviewInput,
                user=Depends(member_resolver), service=Depends(get_task_review_service), sessions=Depends(get_review_sessions)):
        row = scope(service, user, team_id, project_id, task_id, basis_id)
        source = source_for(service, user, row, payload.submission_id)
        require_saved_session(service, sessions, user, row, source)
        return service.preview(user, row.id, source.id, payload.title, payload.description, **arguments(payload))

    @router.post(root + '/{basis_id}/change-request')
    def register(team_id: int, project_id: int, task_id: int, basis_id: UUID, payload: ConfirmedReview,
                 user=Depends(member_resolver), service=Depends(get_task_review_service), sessions=Depends(get_review_sessions)):
        row = scope(service, user, team_id, project_id, task_id, basis_id)
        source = source_for(service, user, row, payload.submission_id)
        require_saved_session(service, sessions, user, row, source)
        converted = service.convert(user, row.id, source.id, payload.title, payload.description,
                                    confirmation_id=str(payload.confirmation_id), **arguments(payload))
        return result_public(service, row, source, converted)

    def result_public(service, row, source, converted):
        cr = service.db.get(models.ChangeRequest, converted.change_request_id) if converted and converted.change_request_id else None
        integration = service.db.get(models.ChangeRequestIntegration, cr.id) if cr else None
        diagnostic = service.db.get(models.WorkCollectionDiagnostic, source.id)
        with service.session_factory(row.project_id) as git:
            main_differs = git.read_ref(('heads','main')) not in (None,row.base_commit)
        return {'basis_state': row.state, 'collection_state': source.state,
                'conversion_state': converted.state if converted else 'NOT_REQUESTED',
                'change_request_id': cr.id if cr else None,
                'main_differs': main_differs,
                'error_code': diagnostic.code if diagnostic else None,
                'review_state': (cr.review.decision if cr.review else 'PENDING') if cr else None,
                'integration_state': (integration.status if integration else 'NOT_INTEGRATED') if cr else None,
                'review_required': source.state == 'REVIEW_REQUIRED' or (converted is not None and converted.state in ('PREPARED', 'REVIEW_REQUIRED'))}

    @router.get(root + '/{basis_id}/collections/{submission_id}')
    def result(team_id: int, project_id: int, task_id: int, basis_id: UUID, submission_id: UUID,
               user=Depends(member_resolver), service=Depends(get_task_review_service)):
        row = scope(service, user, team_id, project_id, task_id, basis_id)
        source = source_for(service, user, row, submission_id)
        bound = service.db.get(models.WorkCaptureBinding, source.id)
        if bound is None or bound.basis_id != row.id:raise PublicError(404, 'COLLECTION_NOT_FOUND')
        return result_public(service, row, source, service.db.get(models.TaskReviewConversion, source.id))

    return router
