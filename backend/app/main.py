from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.firebase import verify_firebase_token
from app.db.database import engine, get_db, Base
import app.models as models
import app.schemas as schemas

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