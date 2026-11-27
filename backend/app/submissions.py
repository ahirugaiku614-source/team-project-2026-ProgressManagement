"""One-use permits and durable admission before invoking a trusted collector.

No automatic retry after admission. Uncertain receipts remain REVIEW_REQUIRED.
The database stores only token digests; never log request bodies or receipts.
"""
from datetime import datetime, timedelta
import hashlib
import re
import secrets
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app import models
from app.submission_targets import resolve_submission_target
from app.submission_provider import SubmissionCollection, SubmissionSnapshot, ProviderUnavailable

PERMIT_SECONDS = 120


def token_digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def lock_target(db, actor, team_id, project_id, task_id):
    # Match Stage 1 project lock order; re-read mutable authorization records.
    db.query(models.Project).filter_by(id=project_id).with_for_update().all()
    db.query(models.TeamMember).filter_by(team_id=team_id, user_id=actor.id).with_for_update().all()
    db.query(models.Task).filter_by(id=task_id).with_for_update().all()
    db.query(models.TaskSubmissionTarget).filter_by(task_id=task_id).with_for_update().all()
    db.expire_all()
    return resolve_submission_target(db, actor, team_id=team_id, project_id=project_id, task_id=task_id)


def issue_permit(db, actor, team_id, project_id, task_id, session_id):
    target = lock_target(db, actor, team_id, project_id, task_id)
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    row = models.SubmissionPermit(id=str(uuid4()), token_digest=token_digest(token),
        student_id=actor.id, team_id=team_id, project_id=project_id, task_id=task_id,
        editing_session_id=session_id, folder_path=target.folder_path,
        purpose="TASK_SUBMISSION", created_at=now, expires_at=now + timedelta(seconds=PERMIT_SECONDS))
    db.add(row)
    db.commit()
    return {"permit": token, "expires_at": row.expires_at, "purpose": row.purpose}


def public_submission(row):
    return {name: getattr(row, name) for name in (
        "id", "request_id", "student_id", "team_id", "project_id", "task_id", "state",
        "git_ref", "commit_id", "created_at", "submitted_at", "file_count", "total_bytes")}


def submit(db, actor, team_id, project_id, task_id, payload, provider, *, before_collect=None, before_publish=None, on_failure=None):
    target = lock_target(db, actor, team_id, project_id, task_id)
    digest = token_digest(payload.permit)
    request_id, session_id = str(payload.request_id), str(payload.editing_session_id)
    existing = db.query(models.TaskSubmission).filter_by(
        student_id=actor.id, task_id=task_id, request_id=request_id).first()
    if existing:
        if (existing.team_id, existing.project_id, existing.editing_session_id,
                existing.permit_digest, existing.folder_path) != (
                team_id, project_id, session_id, digest, target.folder_path):
            raise HTTPException(409, "Request identifier already used with different parameters")
        if existing.state == 'SUBMITTED':
            try:
                provider.verify(SubmissionCollection(existing.id, request_id, session_id, target),
                    SubmissionSnapshot(existing.id, target, session_id, existing.git_ref,
                        existing.commit_id, existing.file_count, existing.total_bytes, True))
            except Exception:
                existing.state = 'REVIEW_REQUIRED'
                db.commit()
        return public_submission(existing), False
    permit = db.query(models.SubmissionPermit).filter_by(token_digest=digest).with_for_update().first()
    if permit is None or (permit.student_id, permit.team_id, permit.project_id, permit.task_id,
            permit.editing_session_id, permit.folder_path, permit.purpose) != (
            actor.id, team_id, project_id, task_id, session_id, target.folder_path, "TASK_SUBMISSION"):
        raise HTTPException(403, "Submission permit is invalid")
    now = datetime.utcnow()
    if permit.consumed_at is not None or permit.expires_at <= now:
        raise HTTPException(409, "Submission permit expired or already consumed")
    # Denied runtime never consumes a permit or creates a submission row.
    provider.ensure_ready()
    changed = db.query(models.SubmissionPermit).filter(
        models.SubmissionPermit.id == permit.id,
        models.SubmissionPermit.consumed_at.is_(None),
        models.SubmissionPermit.expires_at > now,
    ).update({"consumed_at": now}, synchronize_session=False)
    if changed != 1:
        raise HTTPException(409, "Submission permit already consumed")
    row = models.TaskSubmission(id=str(uuid4()), student_id=actor.id, team_id=team_id,
        project_id=project_id, task_id=task_id, request_id=request_id,
        editing_session_id=session_id, permit_id=permit.id, permit_digest=digest,
        folder_path=target.folder_path, state="PROCESSING", created_at=now)
    db.add(row)
    try:
        db.commit()  # admission and permit consumption are atomic, before side effects
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Submission request conflicts with an existing request") from None
    row_id = row.id
    try:
        # Reacquire authorization locks after durable admission and before Git work.
        if lock_target(db, actor, team_id, project_id, task_id) != target:
            raise ProviderUnavailable('Submission binding changed before collection')
        admitted = db.get(models.TaskSubmission, row_id, populate_existing=True)
        consumed = db.get(models.SubmissionPermit, admitted.permit_id, populate_existing=True)
        if admitted.state != 'PROCESSING' or consumed is None or consumed.consumed_at is None:
            raise ProviderUnavailable('Submission admission evidence is unavailable')
        # 新しいタスク変更経路だけが基準との事前結合を行う。旧APIの契約は維持する。
        if before_collect is not None:
            before_collect(row_id)
            # 基準結合のcommit後に認可を再取得してから収集を始める。
            if lock_target(db, actor, team_id, project_id, task_id) != target:
                raise ProviderUnavailable('Submission binding changed before collection')
        receipt = provider.collect(SubmissionCollection(row_id, request_id, session_id, target))
        if (not isinstance(receipt, SubmissionSnapshot)
                or receipt.submission_id != row_id or receipt.target != target
                or receipt.editing_session_id != session_id
                or receipt.git_ref != f"refs/task-submissions/{row_id}"
                or not isinstance(receipt.commit_id, str)
                or not re.fullmatch(r"[0-9a-f]{40}", receipt.commit_id)
                or type(receipt.file_count) is not int or not 1 <= receipt.file_count <= 1000
                or type(receipt.total_bytes) is not int or not 0 <= receipt.total_bytes <= 10 * 1024 * 1024
                or receipt.saved_files_confirmed is not True):
            raise ProviderUnavailable("Invalid collector receipt")
        # Revocation or binding change during collection must prevent publication.
        current = lock_target(db, actor, team_id, project_id, task_id)
        if current != target:
            raise ProviderUnavailable("Submission binding changed")
        provider.verify(SubmissionCollection(row_id, request_id, session_id, target), receipt)
        # 収集中に設定が変更され元へ戻った場合も、失効済み基準を公開成功にしない。
        if before_publish is not None:
            before_publish(row_id)
        row = db.get(models.TaskSubmission, row_id, populate_existing=True)
        if row.state != 'PROCESSING':
            raise ProviderUnavailable('Submission state changed during collection')
        row.state = "SUBMITTED"
        row.git_ref, row.commit_id = receipt.git_ref, receipt.commit_id
        row.file_count, row.total_bytes = receipt.file_count, receipt.total_bytes
        row.submitted_at = datetime.utcnow()
        db.commit()
    except Exception as collection_error:
        # Do not call the collector again, erase its snapshot, or free the permit.
        db.rollback()
        row = db.get(models.TaskSubmission, row_id, populate_existing=True)
        if row.state != "SUBMITTED":
            row.state = "REVIEW_REQUIRED"
            db.commit()
        if on_failure is not None:
            on_failure(row_id, collection_error)
    return public_submission(row), True


def reconcile_submission(db, actor, submission_id, provider):
    """Explicit internal reconciliation, not a retry endpoint or automatic resume.

    Requires current authorization and matching durable Git evidence. Never collects
    files or recreates refs. PROCESSING with incomplete evidence stays uncertain.
    """
    row = db.get(models.TaskSubmission, submission_id)
    if row is None or row.student_id != actor.id:
        raise HTTPException(404, 'Submission not found')
    target = lock_target(db, actor, row.team_id, row.project_id, row.task_id)
    if target.folder_path != row.folder_path:
        raise HTTPException(409, 'Submission binding changed')
    request = SubmissionCollection(row.id, row.request_id, row.editing_session_id, target)
    try:
        consumed = db.get(models.SubmissionPermit, row.permit_id, populate_existing=True)
        if (consumed is None or consumed.consumed_at is None
                or (consumed.token_digest, consumed.student_id, consumed.team_id,
                    consumed.project_id, consumed.task_id, consumed.editing_session_id,
                    consumed.folder_path, consumed.purpose) != (
                    row.permit_digest, row.student_id, row.team_id, row.project_id,
                    row.task_id, row.editing_session_id, row.folder_path, 'TASK_SUBMISSION')):
            raise ProviderUnavailable('Consumed permit evidence does not match')
        receipt = provider.recover(request)
        provider.verify(request, receipt)
        if (receipt.submission_id != row.id or receipt.target != target
                or receipt.editing_session_id != row.editing_session_id
                or receipt.git_ref != 'refs/task-submissions/' + row.id
                or not re.fullmatch(r'[0-9a-f]{40}', receipt.commit_id)
                or receipt.saved_files_confirmed is not True
                or type(receipt.file_count) is not int or not 1 <= receipt.file_count <= 1000
                or type(receipt.total_bytes) is not int or not 0 <= receipt.total_bytes <= 10485760):
            raise ProviderUnavailable('Recovery receipt does not match')
        if row.commit_id is not None and (row.git_ref, row.commit_id, row.file_count, row.total_bytes) != (
                receipt.git_ref, receipt.commit_id, receipt.file_count, receipt.total_bytes):
            raise ProviderUnavailable('Database and Git disagree')
        if lock_target(db, actor, row.team_id, row.project_id, row.task_id) != target:
            raise ProviderUnavailable('Submission binding changed during reconciliation')
        row = db.get(models.TaskSubmission, submission_id, populate_existing=True)
        submitted_at = row.submitted_at or datetime.utcnow()
        row.state = 'SUBMITTED'
        row.git_ref, row.commit_id = receipt.git_ref, receipt.commit_id
        row.file_count, row.total_bytes = receipt.file_count, receipt.total_bytes
        row.submitted_at = submitted_at
        db.commit()
    except Exception:
        db.rollback()
        row = db.get(models.TaskSubmission, submission_id)
        row.state = 'REVIEW_REQUIRED'
        db.commit()
    return public_submission(row)
