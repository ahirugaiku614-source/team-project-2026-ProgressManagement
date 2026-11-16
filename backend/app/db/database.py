import os

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

def get_database_url():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing database environment variables: " + ", ".join(missing))

    # URL.create handles passwords containing @, :, / and other URL characters.
    return URL.create(
        "postgresql+psycopg2",
        username=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        host=os.environ.get("POSTGRES_HOST", "db"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ["POSTGRES_DB"],
    )


SQLALCHEMY_DATABASE_URL = get_database_url()

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def create_startup_tables(bind):
    """従来の初期テーブルだけを作り、明示的な移行が必要な追加テーブルは作らない。

    uvicorn --reloadでモデルが再読込されても、レビュー判定の移行を実DBへ
    暗黙に適用しないための境界。判定テーブルは003 SQLで運用者が作成する。
    テストは専用エンジンでmetadata.create_allを明示して全テーブルを用意できる。
    """
    startup_tables = [table for table in Base.metadata.sorted_tables
                      if not table.info.get("requires_explicit_migration", False)]
    Base.metadata.create_all(bind=bind, tables=startup_tables)

# 各 API リクエストで DB セッションを扱い、処理後に自動で閉じる依存関数
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
