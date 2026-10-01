from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# docker-compose.yml で指定した DB 接続情報
# postgresql://<ユーザー名>:<パスワード>@<サービス名(db)>:<ポート>/<DB名>
SQLALCHEMY_DATABASE_URL = "postgresql://postgres:postgrespassword@db:5432/team_progress_db"

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# 各 API リクエストで DB セッションを扱い、処理後に自動で閉じる依存関数
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()