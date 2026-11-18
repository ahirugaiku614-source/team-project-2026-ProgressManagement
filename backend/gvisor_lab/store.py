"""gVisor参照台帳。一時ファイルだけを使用し、既存DB/volume/VM台帳を開かない。"""

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import stat

from gvisor_lab.policy import Budget, Denied, encode, safe


class Store:
    """小規模試験用SQLite。全予約と操作履歴を一つのローカルトランザクションで直列化する。"""

    @safe
    def __init__(self, path):
        """専用markerの台帳だけを再開する。他DBを自動作成/初期化しない。"""
        self.path = Path(path).absolute()
        with self.connection() as db:
            if db.execute("PRAGMA application_id").fetchone()[0] != 0x47564C31:
                raise Denied()
            row = db.execute("SELECT * FROM snapshot WHERE id=1").fetchone()
            if not row or row["marker"] != "gvisor-lab-v1":
                raise Denied()
            self.budget = Budget(**json.loads(row["budget"]))

    @classmethod
    @safe
    def create_new(cls, path, budget):
        """新規一時DBをO_EXCLで作る。既存ファイル/失敗した途中ファイルを削除しない。"""
        if not isinstance(budget, Budget):
            raise Denied()
        path = Path(path).absolute()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        db = sqlite3.connect(str(path))
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA application_id=1196837937")
            db.execute("CREATE TABLE snapshot(id INTEGER PRIMARY KEY CHECK(id=1),marker TEXT NOT NULL,budget TEXT NOT NULL,data TEXT NOT NULL)")
            db.execute("INSERT INTO snapshot VALUES(1,'gvisor-lab-v1',?,?)",
                       (encode(asdict(budget)), encode({"clock": 0, "records": {}, "operations": {}})))
            db.commit()
        finally:
            db.close()
        return cls(path)

    @contextmanager
    def connection(self):
        """通常ファイル/単一linkを確認し、接続終了と例外秘匿を保証する。"""
        db = None
        try:
            before = self.path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Denied()
            db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            after = self.path.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise Denied()
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except Exception:
            raise Denied() from None
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def transaction(self, now):
        """時刻逆行/予算すり替えを拒否し、例外時は予約を一切確定しない。"""
        if type(now) is not int or not 0 < now < 2**62:
            raise Denied()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM snapshot WHERE id=1").fetchone()
                if row["budget"] != encode(asdict(self.budget)):
                    raise Denied()
                data = json.loads(row["data"])
                if set(data) != {"clock", "records", "operations"} or type(data["clock"]) is not int or now < data["clock"]:
                    raise Denied()
                yield data
                data["clock"] = now
                db.execute("UPDATE snapshot SET data=? WHERE id=1", (encode(data),))
                db.commit()
            except BaseException:
                db.rollback()
                raise
