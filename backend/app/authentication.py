"""通常APIの認証依存。実装選択を環境変数や利用者入力で切り替えない。"""
from fastapi import Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security=HTTPBearer()


def verify_firebase_token(res:HTTPAuthorizationCredentials=Security(security)):
    # 共有API定義からのimportだけではcredentialやSDKへアクセスしない。
    # 通常mainは起動時に従来のFirebase初期化を行い、この依存も実Firebaseを使う。
    from app.firebase import verify_firebase_token as actual
    return actual(res)
