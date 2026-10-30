from fastapi import FastAPI, Depends,HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from app.firebase import verify_firebase_token
from app.db.database import engine, get_db, Base
import app.models as models
import app.schemas as schemas
from typing import List
import secrets

# データベーステーブルの自動生成
models.Base.metadata.create_all(bind=engine)

app = FastAPI()

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
