"""個人エディタ用のFirebase認証。既存APIの挙動は変更しない。

IDトークンの署名・期限だけでなくFirebaseの失効・無効化も確認する。
Firebaseの生エラーや受け取ったトークンをログ・HTTP応答へ書かない。
CookieやURLパラメータの値をFirebaseトークンの代わりには使わない。
"""

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials

from app.firebase import auth, security


def verify_editor_firebase_token(res: HTTPAuthorizationCredentials = Security(security)) -> dict:
    try:
        claims = auth.verify_id_token(res.credentials, check_revoked=True)
        if not isinstance(claims, dict) or not isinstance(claims.get("uid"), str) or not claims["uid"]:
            raise ValueError("Missing user identity")
        return claims
    except Exception:
        # 接続障害でも認証を省略しない。エラー文字列にはトークン等が入り得るため返さない。
        raise HTTPException(status_code=401, detail="Authentication required",
                            headers={"WWW-Authenticate": "Bearer"}) from None
