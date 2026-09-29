from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from app.firebase import verify_firebase_token

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

#Firebase 認証が必要な保護されたエンドポイント
@app.get("/api/me")
def get_current_user(current_user: dict = Depends(verify_firebase_token)):
    """
    有効な Firebase ID トークンを持つユーザーのみアクセス可能な API
    """
    return {
        "message": "認証に成功しました！",
        "uid": current_user.get("uid"),
        "email": current_user.get("email"),
    }