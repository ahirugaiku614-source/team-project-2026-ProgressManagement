"""一時データ専用の永続コード/セッション保管庫。本番DBや既存VMレジストリとは独立。

コード消費と新規セッション作成を同一SQLiteトランザクションで確定し、プロセス間の
再利用競合を防ぐ。既存ファイルを上書きしない。実トークンや生のCookieは保存しない。
"""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat

from editor_gateway.contracts import AccessDenied

APP_ID = 0x45475731


class TestSessionStore:
    """私有ローカル一時ファイルを使う参照ストア。NFS・複数ホスト・本番利用は対象外。"""

    def __init__(self, path):
        """専用markerを持つ既存試験DBだけを開く。不明DBへのDDLや修復は行わない。"""
        self.path = Path(path).absolute()
        try:
            with self.connection() as db:
                if db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
                    raise AccessDenied()
                if db.execute("SELECT marker FROM meta WHERE id=1").fetchone()[0] != "gateway-reference-v1":
                    raise AccessDenied()
        except Exception:
            raise AccessDenied() from None

    @classmethod
    def create_new(cls, path):
        """新規一時DBを排他的に作成する。途中失敗時も既存名を削除・再初期化しない。"""
        path = Path(path).absolute()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        db = sqlite3.connect(str(path))
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(f"PRAGMA application_id={APP_ID}")
            db.execute("CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1),marker TEXT,last_ms INTEGER NOT NULL)")
            db.execute("INSERT INTO meta VALUES(1,'gateway-reference-v1',0)")
            db.execute("""CREATE TABLE codes(
              digest TEXT PRIMARY KEY, identity_json TEXT NOT NULL, binding_json TEXT NOT NULL,
              generation INTEGER NOT NULL, host TEXT NOT NULL, challenge TEXT NOT NULL,
              expires_ms INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0,1)))""")
            db.execute("""CREATE TABLE sessions(
              digest TEXT PRIMARY KEY, csrf_digest TEXT NOT NULL, identity_json TEXT NOT NULL,
              binding_json TEXT NOT NULL, generation INTEGER NOT NULL, host TEXT NOT NULL,
              expires_ms INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)))""")
            db.commit()
        finally:
            db.close()
        return cls(path)

    @contextmanager
    def connection(self):
        """通常ファイル・単一hardlinkを確認し、Windowsでも接続を必ず解放する。"""
        db = None
        try:
            before = self.path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise AccessDenied()
            db = sqlite3.connect(str(self.path), isolation_level=None, timeout=5)
            after = self.path.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise AccessDenied()
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except Exception:
            raise AccessDenied() from None
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def transaction(self, now):
        """一回消費と保存を原子的に行う。時計逆行・ロック/DB失敗は許可へ変換しない。"""
        if type(now) is not int or not 0 < now < 2**62:
            raise AccessDenied()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if now < db.execute("SELECT last_ms FROM meta WHERE id=1").fetchone()[0]:
                    raise AccessDenied()
                db.execute("UPDATE meta SET last_ms=? WHERE id=1", (now,))
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
