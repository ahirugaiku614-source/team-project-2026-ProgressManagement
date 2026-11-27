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
from app.submission_provider import SubmissionCollection, SubmissionSnapshot, ProviderUnavailable, EvidenceMismatch

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


_EVIDENCE_COLUMNS = (
    'id', 'student_id', 'team_id', 'project_id', 'task_id', 'request_id',
    'editing_session_id', 'permit_id', 'permit_digest', 'folder_path', 'state',
    'git_ref', 'commit_id', 'file_count', 'total_bytes', 'submitted_at',
)


def _record_values(row):
    # ORMのexpire/rollbackに左右されない、検査時点の比較用コピーを保持する。
    return {name: getattr(row, name) for name in _EVIDENCE_COLUMNS}


def _matching_row(db, expected):
    # 状態だけでなく、検査した本人・要求・対象・証拠もSQLの更新前提にする。
    # 別処理が先に確定した結果を、古いORMオブジェクトから上書きしない。
    return db.query(models.TaskSubmission).filter_by(**expected)


def _mark_evidence_conflict(db, expected):
    """確実な不整合だけを条件付きで記録する。未知の障害には使用しない。"""
    db.rollback()
    changed = _matching_row(db, expected).update(
        {'state': 'REVIEW_REQUIRED'}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise ProviderUnavailable('Submission changed during evidence verification')
    try:
        db.commit()
    except Exception:
        # 降格のcommitも応答不明になり得る。再試行せず、成功を返さない。
        db.rollback()
        raise ProviderUnavailable('Submission verification outcome unavailable') from None
    db.expire_all()
    return db.get(models.TaskSubmission, expected['id'], populate_existing=True)


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
            expected = _record_values(existing)
            try:
                provider.verify(SubmissionCollection(existing.id, request_id, session_id, target),
                    SubmissionSnapshot(existing.id, target, session_id, existing.git_ref,
                        existing.commit_id, existing.file_count, existing.total_bytes, True))
            except EvidenceMismatch:
                existing = _mark_evidence_conflict(db, expected)
            except Exception:
                # 認可拒否・一時的な読取り不能・未知の例外では保存済み状態を
                # 変更しない。ただし照合成功も返さず、固定の503契約へ渡す。
                db.rollback()
                raise ProviderUnavailable('Stored submission verification unavailable') from None
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
        # 新規収集の公開は、履歴の照合と分けて現在の保存状態・期限も要求する。
        # 既存providerは従来のverify契約を維持する。専用検査があるproviderだけを
        # 明示的にそちらへ接続し、例外や不正な検査器からの自動fallbackは行わない。
        publication_verify = getattr(provider, 'verify_collection', provider.verify)
        publication_verify(SubmissionCollection(row_id, request_id, session_id, target), receipt)
        # 収集中に設定が変更され元へ戻った場合も、失効済み基準を公開成功にしない。
        if before_publish is not None:
            before_publish(row_id)
        row = db.get(models.TaskSubmission, row_id, populate_existing=True)
        if row.state != 'PROCESSING':
            raise ProviderUnavailable('Submission state changed during collection')
        publication_expected = _record_values(row)
        if _matching_row(db, publication_expected).update(dict(
                state='SUBMITTED', git_ref=receipt.git_ref, commit_id=receipt.commit_id,
                file_count=receipt.file_count, total_bytes=receipt.total_bytes,
                submitted_at=datetime.utcnow()), synchronize_session=False) != 1:
            raise ProviderUnavailable('Submission state changed during publication')
        db.commit()
        db.refresh(row)
    except Exception as collection_error:
        # Do not call the collector again, erase its snapshot, or free the permit.
        db.rollback()
        row = db.get(models.TaskSubmission, row_id, populate_existing=True)
        if row is None:
            raise ProviderUnavailable('Submission outcome unavailable') from None
        if row.state == "PROCESSING":
            # 新規収集の失敗も、別処理の確定済み結果を古い状態で上書きしない。
            _matching_row(db, _record_values(row)).update(
                {'state': 'REVIEW_REQUIRED'}, synchronize_session=False)
            db.commit()
            db.refresh(row)
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
    expected = _record_values(row)
    try:
        consumed = db.get(models.SubmissionPermit, row.permit_id, populate_existing=True)
        if (consumed is None or consumed.consumed_at is None
                or (consumed.token_digest, consumed.student_id, consumed.team_id,
                    consumed.project_id, consumed.task_id, consumed.editing_session_id,
                    consumed.folder_path, consumed.purpose) != (
                    row.permit_digest, row.student_id, row.team_id, row.project_id,
                    row.task_id, row.editing_session_id, row.folder_path, 'TASK_SUBMISSION')):
            raise EvidenceMismatch('Consumed permit evidence does not match')
        receipt = provider.recover(request)
        provider.verify(request, receipt)
        if (receipt.submission_id != row.id or receipt.target != target
                or receipt.editing_session_id != row.editing_session_id
                or receipt.git_ref != 'refs/task-submissions/' + row.id
                or not re.fullmatch(r'[0-9a-f]{40}', receipt.commit_id)
                or receipt.saved_files_confirmed is not True
                or type(receipt.file_count) is not int or not 1 <= receipt.file_count <= 1000
                or type(receipt.total_bytes) is not int or not 0 <= receipt.total_bytes <= 10485760):
            raise EvidenceMismatch('Recovery receipt does not match')
        if row.commit_id is not None and (row.git_ref, row.commit_id, row.file_count, row.total_bytes) != (
                receipt.git_ref, receipt.commit_id, receipt.file_count, receipt.total_bytes):
            raise EvidenceMismatch('Database and Git disagree')
        if lock_target(db, actor, row.team_id, row.project_id, row.task_id) != target:
            raise ProviderUnavailable('Submission binding changed during reconciliation')
        row = db.get(models.TaskSubmission, submission_id, populate_existing=True)
        if _record_values(row) != expected:
            raise ProviderUnavailable('Submission changed during reconciliation')
        submitted_at = row.submitted_at or datetime.utcnow()
        published = dict(state='SUBMITTED', git_ref=receipt.git_ref, commit_id=receipt.commit_id,
            file_count=receipt.file_count, total_bytes=receipt.total_bytes, submitted_at=submitted_at)
        # SELECTとUPDATEの間に別処理が確定しても、比較した状態・証拠以外を上書きしない。
        if _matching_row(db, expected).update(published, synchronize_session=False) != 1:
            raise ProviderUnavailable('Submission changed during reconciliation')
    except EvidenceMismatch:
        return public_submission(_mark_evidence_conflict(db, expected))
    except Exception:
        db.rollback()
        raise ProviderUnavailable('Submission reconciliation unavailable') from None

    try:
        db.commit()
    except Exception:
        # commit例外は「未確定」と断定できない。まず失敗したトランザクションを
        # 終了し、ORMキャッシュを捨てた新しいトランザクションでDBを再取得する。
        # 再取得にも失敗した場合は503にし、確定済みかもしれない状態を変更しない。
        db.rollback()
        db.expire_all()
        try:
            row = db.get(models.TaskSubmission, submission_id, populate_existing=True)
            if row is None:
                raise ProviderUnavailable('Submission outcome unavailable')
            actual = _record_values(row)
            # 同じ主体・対象・要求の記録だけを照合する。別処理による確定は
            # 同一の証拠である場合だけ受け入れ、状態更新は一切再実行しない。
            immutable = set(_EVIDENCE_COLUMNS) - set(published)
            if any(actual[name] != expected[name] for name in immutable):
                raise ProviderUnavailable('Submission identity changed')
            if lock_target(db, actor, row.team_id, row.project_id, row.task_id) != target:
                raise ProviderUnavailable('Submission authorization changed')
            verified = provider.recover(request)
            provider.verify(request, verified)
            if verified != receipt:
                raise ProviderUnavailable('Submission outcome evidence changed')
            # 認可・Git読取り中の変更も再取得して検出する。最初のSELECT結果や
            # expire前のORM値だけで、別処理の結果を今回の成功と判定しない。
            row = db.get(models.TaskSubmission, submission_id, populate_existing=True)
            if row is None:
                raise ProviderUnavailable('Submission outcome unavailable')
            actual = _record_values(row)
            if any(actual[name] != expected[name] for name in immutable):
                raise ProviderUnavailable('Submission identity changed')
            if actual['state'] == 'SUBMITTED':
                if any(actual[name] != published[name] for name in
                       ('git_ref', 'commit_id', 'file_count', 'total_bytes')):
                    raise ProviderUnavailable('Submission outcome disagrees')
                result = public_submission(row)
                db.rollback()  # 読取り専用の再確認を終了する。commitを再試行しない。
                return result
            if actual != expected:
                raise ProviderUnavailable('Submission outcome changed')
            # commit前失敗をDBで確認できた場合も再確定・再収集を行わない。
            # 503で明示的な結果再確認を求め、既存の障害状態と証拠を保持する。
            raise ProviderUnavailable('Submission commit was not confirmed')
        except Exception:
            db.rollback()
            raise ProviderUnavailable('Submission commit outcome unavailable') from None
    db.expire_all()
    return public_submission(db.get(models.TaskSubmission, submission_id, populate_existing=True))
