"""課題提出の識別・認可だけ。本文取得、提出API、Git操作は行わない。

actorは既存のFirebase/Team依存関数で認証されたUserに限定する。
登録サービスのcommit/rollbackは呼出し元の責任。対応表の自動更新はしない。
"""


from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app import models
from app.db.database import get_db


# 値型・例外はDB非依存モジュールと同一のものを再公開する。
from app.submission_scope import (
    TargetInvalid, TargetForbidden, TargetConflict, positive_id,
    normalize_folder_path, SubmissionTarget,
)


def _member(db, actor, team_id):
    if not isinstance(actor, models.User):
        raise TargetForbidden("Authenticated user required")
    positive_id(actor.id)
    if db.get(models.User, actor.id) is None:
        raise TargetForbidden("Authenticated user required")
    member = db.query(models.TeamMember).filter_by(team_id=team_id, user_id=actor.id).first()
    if member is None:
        raise TargetForbidden("Current team membership required")
    return member


def _project_task(db, team_id, project_id, task_id, *, lock=False):
    for value in (team_id, project_id, task_id):
        positive_id(value)
    query = db.query(models.Project).filter_by(id=project_id, team_id=team_id).populate_existing()
    # 対応登録の重複/包含パス検査をProject単位で直列化する（本番PostgreSQL）。
    project = (query.with_for_update() if lock else query).first()
    task = db.query(models.Task).filter_by(id=task_id, team_id=team_id).populate_existing().first()
    if project is None or task is None:
        raise TargetInvalid("Task/project/team does not match")
    return project, task


def register_submission_target(db, actor, *, team_id, project_id, task_id, student_id, folder_path):
    positive_id(team_id)
    member = _member(db, actor, team_id)
    if member.role != "owner":
        raise TargetForbidden("Only the team owner can register submission targets")
    folder = normalize_folder_path(folder_path)
    positive_id(student_id)
    _, task = _project_task(db, team_id, project_id, task_id, lock=True)
    if task.assignee_id != student_id or db.query(models.TeamMember).filter_by(
        team_id=team_id, user_id=student_id,
    ).first() is None:
        raise TargetForbidden("Assigned student membership required")
    existing = db.get(models.TaskSubmissionTarget, task_id)
    if existing is not None:
        if (existing.project_id, existing.student_id, existing.folder_path) != (project_id, student_id, folder):
            raise TargetConflict("Target already registered; explicit reassignment is required")
        return existing
    for sibling in db.query(models.TaskSubmissionTarget).filter_by(project_id=project_id, student_id=student_id):
        other = normalize_folder_path(sibling.folder_path)
        if folder == other or folder.startswith(other + "/") or other.startswith(folder + "/"):
            raise TargetConflict("Overlapping submission folders")
    row = models.TaskSubmissionTarget(task_id=task_id, project_id=project_id, student_id=student_id,
                                      folder_path=folder, registered_by=actor.id)
    db.add(row)
    db.flush()
    return row


def resolve_submission_target(db, actor, *, team_id, project_id, task_id):
    positive_id(team_id)
    _member(db, actor, team_id)
    _, task = _project_task(db, team_id, project_id, task_id)
    row = db.get(models.TaskSubmissionTarget, task_id, populate_existing=True)
    if row is None:
        raise TargetConflict("Submission target is not configured")
    if row.project_id != project_id:
        raise TargetInvalid("Task/project binding does not match")
    # Team ownerにも他学生の提出権限を与えない。旧Task更新だけで権限を獲得できない。
    if row.student_id != actor.id or task.assignee_id != actor.id:
        raise TargetForbidden("Submission is restricted to the registered assigned student")
    return SubmissionTarget(team_id, project_id, task_id, actor.id, row.folder_path)


def create_submission_target_resolver(team_member_resolver, project_resolver):
    """既存main.pyの認証/所属・Project依存関数へ接続する。APIは登録しない。"""
    def resolve(team_id: int, project_id: int, task_id: int,
                user: models.User = Depends(team_member_resolver),
                project: models.Project = Depends(project_resolver), db: Session = Depends(get_db)):
        if project.id != project_id or project.team_id != team_id:
            raise HTTPException(status_code=404, detail="Submission target not found")
        try:
            return resolve_submission_target(db, user, team_id=team_id, project_id=project_id, task_id=task_id)
        except TargetForbidden:
            raise HTTPException(status_code=403, detail="Submission is not permitted") from None
        except TargetInvalid:
            raise HTTPException(status_code=404, detail="Submission target not found") from None
        except TargetConflict:
            raise HTTPException(status_code=409, detail="Submission target is not configured") from None
    return resolve
