"""タスク限定収集を既存ChangeRequestへ接続する公開入口。

本文・OID・ref・絶対パスは入出力に含めない。依存注入はサーバー設定だけで行い、
HTTPパラメーターから模擬providerや模擬検証器を選択する機能は設けない。
"""
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
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


def get_review_sessions():
    return DeniedSessionVerifier()


def get_task_review_service(db: Session = Depends(get_db), storage=Depends(get_project_storage),
                            workspaces=Depends(get_workspaces)):
    require_lifecycle(db)
    return TaskReviewService(db, storage, workspaces=workspaces)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    submission_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    deletion_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    previous_request_id: int | None = Field(default=None, gt=0)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


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
        if isinstance(evidence, VerifiedSession) and evidence.saved is False:
            raise PublicError(409, 'UNSAVED_CHANGES')
        # 同一対象・session・世代・有効期限も検査する。isDirtyのクライアント申告は使わない。
        class FixedEvidence:
            def verify(self, *args):return evidence
        check_session(FixedEvidence(), target, source.editing_session_id, basis.source_generation, int(time.time()))
    except PublicError:
        raise
    except Exception:
        raise PublicError(503, 'EDIT_SESSION_UNVERIFIED') from None


def create_task_review_router(member_resolver):
    router = APIRouter(route_class=SafeRoute)
    root = '/api/teams/{team_id}/projects/{project_id}/tasks/{task_id}/work-bases'

    def scope(service, user, team, project, task, basis_id=None):
        require_lifecycle(service.db)
        lock_target(service.db, user, team, project, task)
        if basis_id is None:return None
        row = service.db.get(models.WorkBasis, str(basis_id), populate_existing=True)
        if row is None or (row.member_id, row.team_id, row.project_id, row.task_id) != (user.id, team, project, task):
            raise PublicError(404, 'WORK_BASIS_NOT_FOUND')
        return row

    def basis_public(row):
        return {'id': row.id, 'state': row.state, 'started_at': row.created_at}

    def source_for(service, user, basis, sid):
        row = service.db.get(models.TaskSubmission, str(sid), populate_existing=True)
        if row is None or (row.student_id, row.team_id, row.project_id, row.task_id) != (user.id, basis.team_id, basis.project_id, basis.task_id):
            raise PublicError(404, 'COLLECTION_NOT_FOUND')
        return row

    @router.post(root, status_code=201)
    def start(team_id: int, project_id: int, task_id: int, payload: EmptyInput, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        scope(service, user, team_id, project_id, task_id)
        return basis_public(service.start(user, team_id, project_id, task_id))

    @router.get(root + '/current')
    def current(team_id: int, project_id: int, task_id: int, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        scope(service, user, team_id, project_id, task_id)
        rows = service.db.query(models.WorkBasis).filter_by(member_id=user.id, team_id=team_id, project_id=project_id, task_id=task_id).order_by(models.WorkBasis.created_at.desc()).all()
        return {'basis': basis_public(rows[0]) if rows else None}

    @router.get(root + '/{basis_id}')
    def basis(team_id: int, project_id: int, task_id: int, basis_id: UUID, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        return basis_public(scope(service, user, team_id, project_id, task_id, basis_id))

    @router.get(root + '/{basis_id}/deletion-candidates')
    def candidates(team_id: int, project_id: int, task_id: int, basis_id: UUID, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        scope(service, user, team_id, project_id, task_id, basis_id)
        # blob OIDやモードは管理側の削除証拠だけに保存し、HTTPへは出さない。
        return {'candidates': [{'id': k, 'path': v['path']} for k, v in service.deletion_candidates(user, str(basis_id)).items()]}

    @router.post(root + '/{basis_id}/deletion-intents', status_code=201)
    def deletion(team_id: int, project_id: int, task_id: int, basis_id: UUID, payload: DeletionInput, user=Depends(member_resolver), service=Depends(get_task_review_service)):
        row = scope(service, user, team_id, project_id, task_id, basis_id)
        source_for(service, user, row, payload.submission_id)
        result = service.confirm_deletion(user, str(basis_id), str(payload.submission_id), str(payload.candidate_id), str(payload.confirmation_id))
        return {'deletion_id': result.id, 'path': result.path}

    @router.post(root + '/{basis_id}/collections', status_code=202)
    def collect(team_id: int, project_id: int, task_id: int, basis_id: UUID, payload: SubmissionRequest,
                user=Depends(member_resolver), service=Depends(get_task_review_service),
                sessions=Depends(get_review_sessions), provider=Depends(get_submission_provider)):
        row = scope(service, user, team_id, project_id, task_id, basis_id)
        if payload.has_unsaved_changes:raise PublicError(409, 'UNSAVED_CHANGES')
        with service.session_factory(row.project_id) as git:
            service._basis(git, user, row.id)
        # セッションは許可文字列とは独立して検証する。収集受付後、収集前に基準を固定する。
        from types import SimpleNamespace
        require_saved_session(service, sessions, user, row, SimpleNamespace(editing_session_id=str(payload.editing_session_id)))
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
