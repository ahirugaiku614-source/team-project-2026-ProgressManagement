import os
import firebase_admin
from firebase_admin import credentials, auth
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# 秘密鍵ファイルのパス（backend/ フォルダ直下の firebase-credentials.json）
CREDENTIAL_PATH = os.path.join(os.path.dirname(__file__), "..", "firebase-credentials.json")

# Firebase Admin SDK の初期化
if not firebase_admin._apps:
    if os.path.exists(CREDENTIAL_PATH):
        cred = credentials.Certificate(CREDENTIAL_PATH)
        firebase_admin.initialize_app(cred)
    else:
        raise RuntimeError(f"Firebase credentials file not found at: {CREDENTIAL_PATH}")

# Authorization: Bearer <TOKEN> ヘッダーを取得するためのセキュリティ設定
security = HTTPBearer()

def verify_firebase_token(res: HTTPAuthorizationCredentials = Security(security)) -> dict:
    """
    リクエストヘッダーから ID トークンを取得・検証する関数
    """
    token = res.credentials
    try:
        # トークンの検証（期限切れや改ざんをチェック）
        decoded_token = auth.verify_id_token(token)
        return decoded_token  # 検証成功時にユーザー情報を返す
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired authentication token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )