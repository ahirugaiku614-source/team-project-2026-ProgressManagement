"""認証方式に依存しない既存API定義。通常入口と隔離合成入口が同じ処理を使う。

認可・DB・Gitの製品処理は移動前のまま。合成認証はこのmodule自身では選択しない。
"""
from fastapi import FastAPI, Depends, HTTPException, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.authentication import verify_firebase_token
from app.db.database import engine, get_db, Base, create_startup_tables
import app.models as models
import app.schemas as schemas
from typing import List
import secrets
from sqlalchemy.exc import SQLAlchemyError
from app.change_requests import submit_change_request, change_request_diff
from app.change_request_reviews import decide_change_request, ReviewForbidden, ReviewNotFound
from app.change_request_integration import integrate_change_request
from app.review_git import ReviewInvalid, ReviewLimit
from app.project_storage import get_project_storage, ProjectStorage, StorageConflict, StorageUnavailable

# 従来の初期テーブルだけを自動生成する。レビュー判定テーブルは自動reloadで
# 稼働DBへ作成せず、003マイグレーションを明示的に適用した場合だけ利用する。
create_startup_tables(engine)

app = FastAPI()


@app.exception_handler(RequestValidationError)
async def safe_validation_error(request, error):
    try:
        return await request_validation_exception_handler(request, error)
    except UnicodeError:
        # JSONの不正なUnicodeをエラー本文に再表示して500になることを防ぐ。
        return JSONResponse(status_code=422, content={"detail": "Input must be valid UTF-8"})

# Vue.js (http://localhost:5173) からの通信を許可する CORS 設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "FastAPI is running"}

# Firebase認証 ＋ PostgreSQLへのユーザー自動同期処理
@app.get("/api/me", response_model=schemas.UserResponse)
def get_or_create_current_user(
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db)
):
    """
    有効な Firebase ID トークンを持つユーザーを同期・返却する API
    """
    firebase_uid = current_user.get("uid")
    email = current_user.get("email")

    # name がトークンに存在しない場合のデフォルト値設定
    name = current_user.get("name") or (email.split("@")[0] if email else "User")

    # DBから既存ユーザーを検索
    user = db.query(models.User).filter(models.User.firebase_uid == firebase_uid).first()

    # 存在しない場合は新規登録
    if not user:
        user = models.User(
            firebase_uid=firebase_uid,
            email=email,
            name=name
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    return user

# --- タスク作成 (POST /api/tasks) ---
@app.post("/api/tasks", response_model=schemas.TaskResponse)
def create_task(
    task_in: schemas.TaskCreate,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db)
):
    """
    ログイン中ユーザーの新規タスクを作成
    """
    firebase_uid = current_user.get("uid")
    user = db.query(models.User).filter(models.User.firebase_uid == firebase_uid).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    new_task = models.Task(
        title=task_in.title,
        description=task_in.description,
        status=task_in.status,
        user_id=user.id
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    return new_task

# --- タスク一覧取得 (GET /api/tasks) ---
@app.get("/api/tasks", response_model=List[schemas.TaskResponse])
def get_user_tasks(
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db)
):
    """
    ログイン中ユーザーのタスク一覧を取得
    """
    firebase_uid = current_user.get("uid")
    user = db.query(models.User).filter(models.User.firebase_uid == firebase_uid).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tasks = db.query(models.Task).filter(models.Task.user_id == user.id).all()
    return tasks

# --- タスク更新 (PUT /api/tasks/{task_id}) ---
@app.put("/api/tasks/{task_id}", response_model=schemas.TaskResponse)
def update_task(
    task_id: int,
    task_in: schemas.TaskCreate,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db)
):
    firebase_uid = current_user.get("uid")
    user = db.query(models.User).filter(models.User.firebase_uid == firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    task = db.query(models.Task).filter(models.Task.id == task_id, models.Task.user_id == user.id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task.title = task_in.title
    task.description = task_in.description
    task.status = task_in.status

    db.commit()
    db.refresh(task)
    return task

# --- タスク削除 (DELETE /api/tasks/{task_id}) ---
@app.delete("/api/tasks/{task_id}")
def delete_task(
    task_id: int,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db)
):
    firebase_uid = current_user.get("uid")
    user = db.query(models.User).filter(models.User.firebase_uid == firebase_uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    task = db.query(models.Task).filter(models.Task.id == task_id, models.Task.user_id == user.id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    db.delete(task)
    db.commit()
    return {"message": "Task deleted successfully"}


@app.post("/api/teams", response_model=schemas.TeamResponse)
def create_team(
    team_in: schemas.TeamCreate,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    # Firebase UIDからログインユーザーを取得
    firebase_uid = current_user.get("uid")

    user = (
        db.query(models.User)
        .filter(models.User.firebase_uid == firebase_uid)
        .first()
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found. Please access /api/me first."
        )

    # チームを作成
    new_team = models.Team(
        name=team_in.name,
        description=team_in.description,
        invite_code=secrets.token_urlsafe(16),
    )

    try:
        db.add(new_team)
        db.flush()  # INSERTを実行してチームIDを取得

        # 作成者をオーナーとして登録
        new_member = models.TeamMember(
            team_id=new_team.id,
            user_id=user.id,
            role="owner",
        )

        db.add(new_member)
        db.commit()
        db.refresh(new_team)

        return new_team

    except Exception:
        db.rollback()
        raise

#  所属チーム一覧取得 (GET /api/teams)
@app.get("/api/teams", response_model=List[schemas.TeamResponse])
def get_user_teams(
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    # Firebase UIDからログインユーザーを取得
    firebase_uid = current_user.get("uid")

    user = (
        db.query(models.User)
        .filter(models.User.firebase_uid == firebase_uid)
        .first()
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found. Please access /api/me first."
        )

    # TeamMemberを通して所属チームを取得
    teams = (
        db.query(models.Team)
        .join(
            models.TeamMember,
            models.Team.id == models.TeamMember.team_id,
        )
        .filter(models.TeamMember.user_id == user.id)
        .all()
    )

    return teams



# --- 招待コードによるチーム参加 (POST /api/teams/join) ---
@app.post("/api/teams/join", response_model=schemas.TeamResponse)
def join_team(
    join_in: schemas.TeamJoinRequest,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    # Firebase UIDからログインユーザーを取得
    firebase_uid = current_user.get("uid")

    user = (
        db.query(models.User)
        .filter(models.User.firebase_uid == firebase_uid)
        .first()
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found. Please access /api/me first."
        )

    # 招待コードからチームを検索
    team = (
        db.query(models.Team)
        .filter(models.Team.invite_code == join_in.invite_code)
        .first()
    )

    if not team:
        raise HTTPException(
            status_code=404,
            detail="Invalid invitation code."
        )

    # すでに所属しているか確認
    existing_member = (
        db.query(models.TeamMember)
        .filter(
            models.TeamMember.team_id == team.id,
            models.TeamMember.user_id == user.id,
        )
        .first()
    )

    if existing_member:
        raise HTTPException(
            status_code=409,
            detail="You are already a member of this team."
        )

    # チームメンバーとして登録
    new_member = models.TeamMember(
        team_id=team.id,
        user_id=user.id,
        role="member",
    )

    try:
        db.add(new_member)
        db.commit()
        db.refresh(team)

        return team

    except Exception:
        db.rollback()
        raise



# --- チームメンバー一覧取得 (GET /api/teams/{team_id}/members) ---
@app.get(
    "/api/teams/{team_id}/members",
    response_model=List[schemas.TeamMemberResponse]
)
def get_team_members(
    team_id: int,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    # Firebase UIDからログインユーザーを取得
    firebase_uid = current_user.get("uid")

    user = (
        db.query(models.User)
        .filter(models.User.firebase_uid == firebase_uid)
        .first()
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found."
        )

    # ログインユーザーがチームに所属しているか確認
    membership = (
        db.query(models.TeamMember)
        .filter(
            models.TeamMember.team_id == team_id,
            models.TeamMember.user_id == user.id,
        )
        .first()
    )

    if not membership:
        raise HTTPException(
            status_code=403,
            detail="You are not a member of this team."
        )

    # チームに所属するメンバー一覧を取得
    members = (
        db.query(
            models.TeamMember.user_id,
            models.User.name,
            models.User.email,
            models.TeamMember.role,
            models.TeamMember.joined_at,
        )
        .join(
            models.User,
            models.TeamMember.user_id == models.User.id,
        )
        .filter(models.TeamMember.team_id == team_id)
        .all()
    )

    return members


def get_project_team_member(
    team_id: int,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    """プロジェクトの操作前に、ユーザー・チーム・所属を確認する。"""
    user = db.query(models.User).filter(
        models.User.firebase_uid == current_user.get("uid")
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    team = db.query(models.Team).filter(models.Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    membership = db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id,
        models.TeamMember.user_id == user.id,
    ).first()
    if not membership:
        raise HTTPException(
            status_code=403,
            detail="You are not a member of this team.",
        )
    return user


@app.post(
    "/api/teams/{team_id}/projects",
    response_model=schemas.ProjectResponse,
    status_code=201,
)
def create_team_project(
    team_id: int,
    project_in: schemas.ProjectCreate,
    user: models.User = Depends(get_project_team_member),
    db: Session = Depends(get_db),
):
    new_project = models.Project(
        name=project_in.name,
        description=project_in.description,
        team_id=team_id,
        created_by=user.id,
    )
    try:
        db.add(new_project)
        db.commit()
        db.refresh(new_project)
    except Exception:
        db.rollback()
        raise
    return new_project


@app.get(
    "/api/teams/{team_id}/projects",
    response_model=List[schemas.ProjectResponse],
)
def get_team_projects(
    team_id: int,
    user: models.User = Depends(get_project_team_member),
    db: Session = Depends(get_db),
):
    return db.query(models.Project).filter(
        models.Project.team_id == team_id,
    ).order_by(models.Project.created_at.desc(), models.Project.id.desc()).all()


def get_accessible_project(
    team_id: int,
    project_id: int,
    user: models.User = Depends(get_project_team_member),
    db: Session = Depends(get_db),
):
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.team_id == team_id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# 稼働基盤未準備の個人エディタ用制御API。既存の所属・Project対応の解決処理を再利用する。
# 起動・停止・セッション発行は常に拒否し、保存領域や既存code-serverには触れない。
# Firebase固有の旧個人エディタrouterは通常main側だけで登録する。
# 合成MVPはtask_review_apiの経路を使い、旧起動経路を有効にしない。

from app.submission_api import create_submission_router
app.include_router(create_submission_router(get_project_team_member))

# 新しい入口も既存Firebase/Team認証を使用する。模擬実装はHTTPから選択できない。
from app.task_review_api import create_task_review_router
app.include_router(create_task_review_router(get_project_team_member))

# 短時間Python実行は独立したAPIとして登録する。既定workerは未接続で常に503。
# 既存個人エディタの実行禁止ゲートやDocker設定は変更しない。
# Firebase固有の実行APIも通常mainだけで登録する。合成変更管理ではコードを実行しない。


def project_storage_operation(storage, project_id, user_id=None, initialize=False):
    try:
        if initialize:
            return storage.initialize(project_id, user_id)
        return storage.status(project_id, user_id)
    except StorageConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except StorageUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=503, detail="Project storage is unavailable") from error


@app.post("/api/teams/{team_id}/projects/{project_id}/repository/init", response_model=schemas.ProjectRepositoryState)
def initialize_project_repository(
    project: models.Project = Depends(get_accessible_project),
    storage: ProjectStorage = Depends(get_project_storage),
):
    return project_storage_operation(storage, project.id, initialize=True)


@app.get("/api/teams/{team_id}/projects/{project_id}/repository", response_model=schemas.ProjectRepositoryState)
def get_project_repository(
    project: models.Project = Depends(get_accessible_project),
    storage: ProjectStorage = Depends(get_project_storage),
):
    return project_storage_operation(storage, project.id)


@app.post("/api/teams/{team_id}/projects/{project_id}/workspace/init", response_model=schemas.ProjectWorkspaceState)
def initialize_project_workspace(
    project: models.Project = Depends(get_accessible_project),
    user: models.User = Depends(get_project_team_member),
    storage: ProjectStorage = Depends(get_project_storage),
):
    return project_storage_operation(storage, project.id, user.id, initialize=True)


@app.get("/api/teams/{team_id}/projects/{project_id}/workspace", response_model=schemas.ProjectWorkspaceState)
def get_project_workspace(
    project: models.Project = Depends(get_accessible_project),
    user: models.User = Depends(get_project_team_member),
    storage: ProjectStorage = Depends(get_project_storage),
):
    return project_storage_operation(storage, project.id, user.id)


def review_operation(operation):
    """サーバーのパスやSQL、Gitの生出力を利用者へ漏らさない。"""
    try:
        return operation()
    except ReviewInvalid as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ReviewLimit as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except UnicodeError as error:
        raise HTTPException(status_code=422, detail="Review files or references must be valid UTF-8") from error
    except StorageConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except StorageUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (OSError, SQLAlchemyError) as error:
        raise HTTPException(status_code=503, detail="Review storage failed; retry with the same request key") from error


@app.post("/api/teams/{team_id}/projects/{project_id}/change-requests", response_model=schemas.ChangeRequestResponse, status_code=201)
def create_change_request(
    payload: schemas.ChangeRequestCreate,
    response: Response,
    project: models.Project = Depends(get_accessible_project),
    user: models.User = Depends(get_project_team_member),
    storage: ProjectStorage = Depends(get_project_storage),
    db: Session = Depends(get_db),
):
    row, created = review_operation(lambda: submit_change_request(db, storage, project.id, user.id, payload))
    response.status_code = 201 if created else 200
    return row


@app.get("/api/teams/{team_id}/projects/{project_id}/change-requests", response_model=List[schemas.ChangeRequestResponse])
def list_change_requests(
    project: models.Project = Depends(get_accessible_project),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return db.query(models.ChangeRequest).filter_by(project_id=project.id).order_by(
        models.ChangeRequest.created_at.desc(), models.ChangeRequest.id.desc(),
    ).offset(offset).limit(limit).all()


def get_accessible_change_request(
    change_request_id: int,
    project: models.Project = Depends(get_accessible_project),
    db: Session = Depends(get_db),
):
    row = db.query(models.ChangeRequest).filter_by(id=change_request_id, project_id=project.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Change request not found")
    return row


@app.get("/api/teams/{team_id}/projects/{project_id}/change-requests/{change_request_id}", response_model=schemas.ChangeRequestResponse)
def get_change_request(row: models.ChangeRequest = Depends(get_accessible_change_request)):
    return row


@app.get("/api/teams/{team_id}/projects/{project_id}/change-requests/{change_request_id}/diff", response_model=schemas.ChangeRequestDiffResponse)
def get_change_request_diff(
    row: models.ChangeRequest = Depends(get_accessible_change_request),
    storage: ProjectStorage = Depends(get_project_storage),
):
    return review_operation(lambda: change_request_diff(storage, row))


def review_decision_operation(operation):
    """判定処理の失敗をHTTPへ変換する。DB・パス・Gitの生のエラーは公開しない。

    コミット後の応答だけ失われた可能性もあるため、503時には再送を促すだけでなく
    詳細APIで確定結果を確認するよう案内する。既存の提出APIの再試行仕様は変えない。
    """
    try:
        return operation()
    except ReviewForbidden as error:
        raise HTTPException(status_code=403, detail="You cannot review this request") from error
    except ReviewNotFound as error:
        raise HTTPException(status_code=404, detail="Change request not found") from error
    except (StorageConflict, ReviewInvalid, ReviewLimit, UnicodeError) as error:
        raise HTTPException(status_code=409, detail="Review state or Git snapshot is inconsistent") from error
    except (StorageUnavailable, SQLAlchemyError, OSError) as error:
        raise HTTPException(status_code=503, detail="Review decision unavailable; check the request detail before retrying") from error


@app.post("/api/teams/{team_id}/projects/{project_id}/change-requests/{change_request_id}/approve", response_model=schemas.ChangeRequestResponse)
def approve_change_request(
    payload: schemas.ChangeRequestApprove | None = None,
    row: models.ChangeRequest = Depends(get_accessible_change_request),
    project: models.Project = Depends(get_accessible_project),
    user: models.User = Depends(get_project_team_member),
    storage: ProjectStorage = Depends(get_project_storage),
    db: Session = Depends(get_db),
):
    """他メンバーがOPEN/PENDINGの提出を承認する。Git mainへの統合は行わない。

    bodyは不要で、{}も許可する。判定者IDはpayloadから取らず、Firebase UIDから
    解決したUserを使う。ProjectとTeamの対応・所属は既存の依存関数で先に確認する。
    """
    return review_decision_operation(lambda: decide_change_request(
        db, storage, project.team_id, project.id, row.id, user.id, "APPROVED",
    ))


@app.post("/api/teams/{team_id}/projects/{project_id}/change-requests/{change_request_id}/request-changes", response_model=schemas.ChangeRequestResponse)
def request_change_request_changes(
    payload: schemas.ChangeRequestChanges,
    row: models.ChangeRequest = Depends(get_accessible_change_request),
    project: models.Project = Depends(get_accessible_project),
    user: models.User = Depends(get_project_team_member),
    storage: ProjectStorage = Depends(get_project_storage),
    db: Session = Depends(get_db),
):
    """理由付きで差し戻す。提出済みスナップショットを編集・差し替えない。

    差し戻し後の修正は既存の提出APIで新しい依頼として送る。今回の依頼の判定を
    PENDINGへ戻したり、承認へ上書きしたりする操作は提供しない。
    """
    return review_decision_operation(lambda: decide_change_request(
        db, storage, project.team_id, project.id, row.id, user.id,
        "CHANGES_REQUESTED", payload.reason,
    ))


def integration_operation(operation):
    """統合の内部エラーは公開しない。503は同じ依頼の状態確認・再送を案内する。

    Gitが更新済みでも503となる場合があるため、「未統合」とは断定しない。
    不整合の409では自動上書きせず、利用者・管理者が確認できる状態を維持する。
    """
    try:
        return operation()
    except ReviewForbidden as error:
        raise HTTPException(status_code=403, detail="You cannot integrate this request") from error
    except ReviewNotFound as error:
        raise HTTPException(status_code=404, detail="Change request not found") from error
    except (StorageConflict, ReviewInvalid, ReviewLimit, UnicodeError, ValueError) as error:
        raise HTTPException(status_code=409, detail="Integration conflict; check the request and contact an administrator if recovery is needed") from error
    except (StorageUnavailable, SQLAlchemyError, OSError) as error:
        raise HTTPException(status_code=503, detail="Integration unavailable; check integration status and retry the same request") from error


@app.post("/api/teams/{team_id}/projects/{project_id}/change-requests/{change_request_id}/integrate",
          response_model=schemas.ChangeRequestIntegrationResponse)
def integrate_approved_change_request(
    payload: schemas.ChangeRequestIntegrate | None = None,
    row: models.ChangeRequest = Depends(get_accessible_change_request),
    project: models.Project = Depends(get_accessible_project),
    user: models.User = Depends(get_project_team_member),
    storage: ProjectStorage = Depends(get_project_storage),
    db: Session = Depends(get_db),
):
    """承認済みの固定スナップショットだけを共有mainへ設定する。本文は不要。

    統合者ID・パス・Git操作は指定させず、Firebase UIDから解決した本人を使う。
    所属や承認はサービス内でもDBロック下で読み直す。個人作業領域は変更しない。
    """
    return integration_operation(lambda: integrate_change_request(
        db, storage, project.team_id, project.id, row.id, user.id,
    ))


@app.get("/api/teams/{team_id}/projects/{project_id}/change-requests/{change_request_id}/integration",
         response_model=schemas.ChangeRequestIntegrationResponse | None)
def get_change_request_integration(
    row: models.ChangeRequest = Depends(get_accessible_change_request),
    db: Session = Depends(get_db),
):
    """nullは未準備、PREPAREDはGit更新済みかもしれない、COMPLETEDは記録確定。

    認証・所属・Project対応は通常の詳細APIと同じ。読み取りでGitを修復しない。
    PREPAREDの確定には、元の統合者が同じPOSTを再送して照合する必要がある。
    """
    return integration_operation(lambda: db.query(models.ChangeRequestIntegration).filter_by(change_request_id=row.id).first())


#チーム共有タスク作成 (POST /api/teams/{team_id}/tasks)
@app.post(
    "/api/teams/{team_id}/tasks",
    response_model=schemas.TeamTaskResponse
)
def create_team_task(
    team_id: int,
    task_in: schemas.TeamTaskCreate,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    # ログインユーザーを取得
    firebase_uid = current_user.get("uid")

    user = db.query(models.User).filter(
        models.User.firebase_uid == firebase_uid
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # チームの存在確認
    team = db.query(models.Team).filter(
        models.Team.id == team_id
    ).first()

    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # ログインユーザーのチーム所属確認
    membership = db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id,
        models.TeamMember.user_id == user.id
    ).first()

    if not membership:
        raise HTTPException(
            status_code=403,
            detail="You are not a member of this team."
        )

    # 担当者が指定されている場合、チームメンバーか確認
    if task_in.assignee_id is not None:
        assignee_membership = db.query(models.TeamMember).filter(
            models.TeamMember.team_id == team_id,
            models.TeamMember.user_id == task_in.assignee_id
        ).first()

        if not assignee_membership:
            raise HTTPException(
                status_code=400,
                detail="Assignee must be a member of this team."
            )

    # ステータスの確認
    try:
        task_status = models.TaskStatus(task_in.status.upper())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid task status."
        )

    # タスクを作成
    new_task = models.Task(
        title=task_in.title,
        description=task_in.description,
        status=task_status,
        user_id=user.id,
        team_id=team_id,
        assignee_id=task_in.assignee_id
    )

    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    return new_task

#チーム共有タスク一覧取得 (GET /api/teams/{team_id}/tasks)
@app.get(
    "/api/teams/{team_id}/tasks",
    response_model=List[schemas.TeamTaskResponse]
)
def get_team_tasks(
    team_id: int,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    # ログインユーザーを取得
    firebase_uid = current_user.get("uid")

    user = db.query(models.User).filter(
        models.User.firebase_uid == firebase_uid
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # チームの存在確認
    team = db.query(models.Team).filter(
        models.Team.id == team_id
    ).first()

    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # チームへの所属確認
    membership = db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id,
        models.TeamMember.user_id == user.id
    ).first()

    if not membership:
        raise HTTPException(
            status_code=403,
            detail="You are not a member of this team."
        )

    # チームに所属するタスクを取得
    tasks = db.query(models.Task).filter(
        models.Task.team_id == team_id
    ).order_by(models.Task.created_at.desc()).all()

    return tasks



# チーム共有タスク更新
@app.put(
    "/api/teams/{team_id}/tasks/{task_id}",
    response_model=schemas.TeamTaskResponse
)
def update_team_task(
    team_id: int,
    task_id: int,
    task_in: schemas.TeamTaskCreate,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    # ログインユーザーを取得
    firebase_uid = current_user.get("uid")
    user = db.query(models.User).filter(
        models.User.firebase_uid == firebase_uid
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # チームへの所属確認
    membership = db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id,
        models.TeamMember.user_id == user.id
    ).first()

    if not membership:
        raise HTTPException(
            status_code=403,
            detail="You are not a member of this team."
        )

    # 更新対象のタスクを取得
    task = db.query(models.Task).filter(
        models.Task.id == task_id,
        models.Task.team_id == team_id
    ).first()

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task not found."
        )

    # 担当者が指定されている場合、チームメンバーか確認
    if task_in.assignee_id is not None:
        assignee_membership = db.query(models.TeamMember).filter(
            models.TeamMember.team_id == team_id,
            models.TeamMember.user_id == task_in.assignee_id
        ).first()

        if not assignee_membership:
            raise HTTPException(
                status_code=400,
                detail="Assignee must be a member of this team."
            )

    # ステータスの確認
    try:
        task_status = models.TaskStatus(task_in.status.upper())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid task status."
        )

    # タスク情報を更新
    task.title = task_in.title
    task.description = task_in.description
    task.status = task_status
    task.assignee_id = task_in.assignee_id

    db.commit()
    db.refresh(task)

    return task



# --- チーム共有タスク削除 ---
@app.delete("/api/teams/{team_id}/tasks/{task_id}")
def delete_team_task(
    team_id: int,
    task_id: int,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    # ログインユーザーを取得
    firebase_uid = current_user.get("uid")
    user = db.query(models.User).filter(
        models.User.firebase_uid == firebase_uid
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # チームへの所属確認
    membership = db.query(models.TeamMember).filter(
        models.TeamMember.team_id == team_id,
        models.TeamMember.user_id == user.id
    ).first()

    if not membership:
        raise HTTPException(
            status_code=403,
            detail="You are not a member of this team."
        )

    # 削除対象のタスクを取得
    task = db.query(models.Task).filter(
        models.Task.id == task_id,
        models.Task.team_id == team_id
    ).first()

    if not task:
        raise HTTPException(
            status_code=404,
            detail="Task not found."
        )

    # 作成者またはチームオーナーか確認
    is_creator = task.user_id == user.id
    is_owner = membership.role == "owner"

    if not (is_creator or is_owner):
        raise HTTPException(
            status_code=403,
            detail="Only the task creator or team owner can delete this task."
        )

    # タスクを削除
    db.delete(task)
    db.commit()

    return {"message": "Team task deleted successfully"}


# コードファイル管理API
# ファイル保存
@app.post("/api/files", response_model=schemas.CodeFileResponse)
def create_code_file(
    file_in: schemas.CodeFileCreate,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    firebase_uid = current_user.get("uid")

    user = db.query(models.User).filter(
        models.User.firebase_uid == firebase_uid
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 同じユーザー内でファイル名が重複していないか確認
    existing = db.query(models.CodeFile).filter(
        models.CodeFile.user_id == user.id,
        models.CodeFile.name == file_in.name
    ).first()

    if existing:
        raise HTTPException(
            status_code=409,
            detail="File name already exists"
        )

    new_file = models.CodeFile(
        user_id=user.id,
        name=file_in.name,
        language=file_in.language,
        content=file_in.content
    )

    db.add(new_file)
    db.commit()
    db.refresh(new_file)

    return new_file


# 自分のファイル一覧取得
@app.get("/api/files", response_model=List[schemas.CodeFileResponse])
def get_code_files(
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    firebase_uid = current_user.get("uid")

    user = db.query(models.User).filter(
        models.User.firebase_uid == firebase_uid
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return db.query(models.CodeFile).filter(
        models.CodeFile.user_id == user.id
    ).order_by(models.CodeFile.created_at.asc()).all()


# ファイル読み込み
@app.get(
    "/api/files/{file_id}",
    response_model=schemas.CodeFileResponse
)
def get_code_file(
    file_id: int,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    firebase_uid = current_user.get("uid")

    user = db.query(models.User).filter(
        models.User.firebase_uid == firebase_uid
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    file = db.query(models.CodeFile).filter(
        models.CodeFile.id == file_id,
        models.CodeFile.user_id == user.id
    ).first()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    return file


# ファイル更新
@app.put(
    "/api/files/{file_id}",
    response_model=schemas.CodeFileResponse
)
def update_code_file(
    file_id: int,
    file_in: schemas.CodeFileUpdate,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    firebase_uid = current_user.get("uid")

    user = db.query(models.User).filter(
        models.User.firebase_uid == firebase_uid
    ).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    file = db.query(models.CodeFile).filter(
        models.CodeFile.id == file_id,
        models.CodeFile.user_id == user.id
    ).first()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    # 名前を変更する場合、重複を確認
    duplicate = db.query(models.CodeFile).filter(
        models.CodeFile.user_id == user.id,
        models.CodeFile.name == file_in.name,
        models.CodeFile.id != file_id
    ).first()

    if duplicate:
        raise HTTPException(
            status_code=409,
            detail="File name already exists"
        )

    file.name = file_in.name
    file.language = file_in.language
    file.content = file_in.content

    db.commit()
    db.refresh(file)

    return file


# コードファイル削除
@app.delete("/api/files/{file_id}")
def delete_code_file(
    file_id: int,
    current_user: dict = Depends(verify_firebase_token),
    db: Session = Depends(get_db),
):
    # Firebase UIDからログインユーザーを取得
    firebase_uid = current_user.get("uid")

    user = db.query(models.User).filter(
        models.User.firebase_uid == firebase_uid
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    # 自分が所有するファイルのみ取得
    file = db.query(models.CodeFile).filter(
        models.CodeFile.id == file_id,
        models.CodeFile.user_id == user.id
    ).first()

    if not file:
        raise HTTPException(
            status_code=404,
            detail="File not found"
        )

    # ファイルを削除
    db.delete(file)
    db.commit()

    return {"message": "File deleted successfully"}
