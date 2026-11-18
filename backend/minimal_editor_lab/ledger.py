"""独立した一時SQLiteで資源予約を直列化する。既存DB/VM台帳は開かない。

状態更新は試験データだけ。実プロセス・VM・コンテナ・ファイル領域は作成しない。
本番永続化の選定、分散台帳、既存managerの置換を行うものではない。
"""

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import stat

from minimal_editor_lab.contracts import Budget, LabDenied

APP_ID = 0x4D454C31


class Ledger:
    """私有一時ファイル用の予約台帳。保存予約と操作意図を同じトランザクションにする。"""

    def __init__(self, path):
        """専用marker付きの試験台帳だけ再開し、既存ファイルを初期化しない。"""
        self.path = Path(path).absolute()
        with self.connection() as db:
            if db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
                raise LabDenied()
            row = db.execute("SELECT * FROM meta WHERE id=1").fetchone()
            if row is None or row["marker"] != "minimal-editor-test-v1":
                raise LabDenied()
            self.budget = Budget(**json.loads(row["budget"]))

    @classmethod
    def create_new(cls, path, budget):
        """排他的に新規DBを作る。失敗したファイルも消去せず、勝手にやり直さない。"""
        if not isinstance(budget, Budget):
            raise LabDenied()
        path = Path(path).absolute()
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        db = sqlite3.connect(str(path))
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(f"PRAGMA application_id={APP_ID}")
            db.execute("CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1),marker TEXT NOT NULL,budget TEXT NOT NULL,last_ms INTEGER NOT NULL CHECK(last_ms>=0))")
            db.execute("INSERT INTO meta VALUES(1,'minimal-editor-test-v1',?,0)", (json.dumps(asdict(budget)),))
            db.execute("""CREATE TABLE environments(
              project_id INTEGER NOT NULL CHECK(project_id>0),user_id INTEGER NOT NULL CHECK(user_id>0),
              team_id INTEGER NOT NULL CHECK(team_id>0),vm_id TEXT NOT NULL UNIQUE,disk_id TEXT NOT NULL UNIQUE,
              cpu INTEGER NOT NULL CHECK(cpu IN (1,2)),memory_mib INTEGER NOT NULL CHECK(memory_mib IN (1024,2048,4096)),
              root_gib INTEGER NOT NULL CHECK(root_gib=16),data_gib INTEGER NOT NULL CHECK(data_gib=20),
              state TEXT NOT NULL CHECK(state IN ('STARTING','RUNNING','STOPPING','STOPPED','QUARANTINED')),
              generation INTEGER NOT NULL CHECK(generation>=0),active_op TEXT,lease_ms INTEGER,
              PRIMARY KEY(project_id,user_id),FOREIGN KEY(active_op) REFERENCES operations(operation_id),
              CHECK((active_op IS NULL AND lease_ms IS NULL) OR (active_op IS NOT NULL AND lease_ms>0)),
              CHECK((state IN ('STARTING','STOPPING') AND active_op IS NOT NULL) OR
                    (state IN ('RUNNING','STOPPED','QUARANTINED') AND active_op IS NULL)))""")
            db.execute("""CREATE TABLE operations(
              operation_id TEXT PRIMARY KEY NOT NULL,project_id INTEGER NOT NULL,user_id INTEGER NOT NULL,team_id INTEGER NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('ENSURE_STARTED','STOP')),generation INTEGER NOT NULL CHECK(generation>0),
              status TEXT NOT NULL CHECK(status IN ('ACTIVE','SUCCEEDED','UNKNOWN')),evidence_json TEXT,quiescence_id TEXT UNIQUE,
              FOREIGN KEY(project_id,user_id) REFERENCES environments(project_id,user_id) ON DELETE RESTRICT,
              UNIQUE(project_id,user_id,generation),
              CHECK((kind='STOP' AND evidence_json IS NOT NULL AND quiescence_id IS NOT NULL) OR
                    (kind='ENSURE_STARTED' AND evidence_json IS NULL AND quiescence_id IS NULL)))""")
            db.commit()
        except Exception:
            raise LabDenied() from None
        finally:
            db.close()
        return cls(path)

    @contextmanager
    def connection(self):
        """通常ファイル/単一linkを照合して開き、例外の内部パスを隠して必ずcloseする。"""
        db = None
        try:
            before = self.path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise LabDenied()
            db = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
            after = self.path.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise LabDenied()
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except Exception:
            raise LabDenied() from None
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def transaction(self, now):
        """複数プロセスの計数/予約をBEGIN IMMEDIATEで一体化。時計逆行/DB失敗は拒否する。"""
        if type(now) is not int or not 0 < now < 2**62:
            raise LabDenied()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM meta WHERE id=1").fetchone()
                if now < row["last_ms"] or json.loads(row["budget"]) != asdict(self.budget):
                    raise LabDenied()
                db.execute("UPDATE meta SET last_ms=? WHERE id=1", (now,))
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def snapshot(self):
        """合成台帳の診断用読取り。disk IDはあるが個人ファイル本体は保持しない。"""
        with self.connection() as db:
            return {name: [dict(row) for row in db.execute("SELECT * FROM " + name + " ORDER BY 1,2")]
                    for name in ("environments", "operations")}
