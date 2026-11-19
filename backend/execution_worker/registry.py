"""worker専用SQLiteで全体4枠と操作履歴を永続化する。既存PostgreSQLは使わない。"""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from uuid import UUID

from execution_worker.contracts import Busy, Denied, Unavailable, canonical, fingerprint, full_id


class Registry:
    """同一hostの複数worker接続をBEGIN IMMEDIATEで直列化する専用台帳。"""

    APPLICATION_ID = 0x50594558

    def __init__(self, path):
        """専用識別子/版を持つ既存台帳だけを開く。別DBを初期化しない。"""
        self.path = Path(path).absolute()
        try:
            with self.connection() as db:
                if db.execute("PRAGMA application_id").fetchone()[0] != self.APPLICATION_ID or db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise Unavailable()
        except Exception:
            raise Unavailable() from None

    @classmethod
    def create_new(cls, path):
        """新規の専用DBだけをO_EXCLで作る。既存/途中ファイルやlockを削除しない。"""
        path = Path(path).absolute()
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            db = sqlite3.connect(str(path))
            try:
                db.executescript(f"""
                    PRAGMA application_id={cls.APPLICATION_ID};
                    PRAGMA user_version=1;
                    PRAGMA synchronous=FULL;
                    CREATE TABLE jobs(
                      execution_id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_hash TEXT NOT NULL,
                      spec TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
                        ('RESERVED','CREATING','RUNNING','CLEANING','COMPLETED','UNKNOWN')),
                      container_id TEXT UNIQUE, revision INTEGER NOT NULL CHECK(revision>0),
                      result TEXT, created_ms INTEGER NOT NULL);
                    CREATE TABLE nonces(nonce TEXT PRIMARY KEY);
                    CREATE TABLE context(profile_hash TEXT NOT NULL);
                """)
            finally:
                db.close()
            return cls(path)
        except Exception:
            raise Unavailable() from None

    @contextmanager
    def connection(self):
        """private directoryの通常ファイルだけに接続し、内部例外を固定拒否にする。"""
        db = None
        try:
            before = self.path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Unavailable()
            db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
            after = self.path.lstat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise Unavailable()
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            yield db
        except (Busy, Denied):
            raise
        except Exception:
            raise Unavailable() from None
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def transaction(self):
        """予約判定と書込みを同じtransactionで確定し、途中例外ならrollbackする。"""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def job_lock(self, execution_id):
        """同一実行の処理と復旧をOS lockで排他する。期限だけで別管理者へ奪取しない。"""
        if type(execution_id) is not UUID:
            raise Unavailable()
        fd = None
        acquired = False
        try:
            lock_path = self.path.with_name(self.path.name + "." + str(execution_id) + ".lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Unavailable()
            if os.name == "nt":
                import msvcrt
                # Windowsのbyte lock用。新規専用lockファイルだけに1 byteを置く。
                if info.st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            yield
        except (Busy, Denied):
            raise
        except Exception:
            raise Unavailable() from None
        finally:
            if fd is not None:
                if acquired:
                    if os.name == "nt":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def admit(self, command, nonce, now, spec):
        """署名nonceの一回消費・同一要求照合・全体4枠の予約を原子的に行う。"""
        key = str(command.execution_id)
        owner, request_hash = canonical(command.owner.model_dump()), fingerprint(command.model_dump(mode="json"))
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM nonces WHERE nonce=?", (nonce,)).fetchone():
                raise Denied()
            db.execute("INSERT INTO nonces VALUES(?)", (nonce,))
            previous = db.execute("SELECT * FROM jobs WHERE execution_id=?", (key,)).fetchone()
            if previous:
                if previous["owner"] != owner or previous["request_hash"] != request_hash:
                    raise Denied()
                return dict(previous), False
            if (db.execute("SELECT 1 FROM jobs WHERE state='UNKNOWN'").fetchone()
                    or db.execute("SELECT count(*) FROM jobs WHERE state!='COMPLETED'").fetchone()[0] >= 4):
                raise Busy()
            db.execute("INSERT INTO jobs VALUES(?,?,?,?, 'RESERVED',NULL,1,NULL,?)",
                       (key, owner, request_hash, canonical(spec), now))
            return dict(db.execute("SELECT * FROM jobs WHERE execution_id=?", (key,)).fetchone()), True

    def bind(self, profile):
        """台帳を単一daemon・image・固定policyへ結び付け、別hostへの誤接続を拒否する。"""
        with self.transaction() as db:
            rows = db.execute("SELECT profile_hash FROM context").fetchall()
            if not rows:
                db.execute("INSERT INTO context VALUES(?)", (profile,))
            elif len(rows) != 1 or rows[0][0] != profile:
                raise Unavailable()

    def get(self, execution_id):
        """完全実行IDで1件だけ読む。host container一覧を操作対象として使わない。"""
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE execution_id=?", (str(execution_id),)).fetchone()
            if row is None:
                raise Unavailable()
            return dict(row)

    def advance(self, row, state, *, container_id=None, result=None):
        """revisionによるCASで古い処理の上書きを拒否する。確定前の削除はしない。"""
        transitions = {"RESERVED": {"CREATING", "COMPLETED", "UNKNOWN"},
                       "CREATING": {"CREATING", "RUNNING", "UNKNOWN"}, "RUNNING": {"CLEANING", "UNKNOWN"},
                       "CLEANING": {"COMPLETED", "UNKNOWN"}, "UNKNOWN": {"CLEANING", "UNKNOWN"}}
        if state not in transitions.get(row["state"], set()) or (container_id is not None and not full_id(container_id)):
            raise Unavailable()
        with self.transaction() as db:
            updated = db.execute("UPDATE jobs SET state=?,container_id=?,revision=revision+1,result=? WHERE execution_id=? AND revision=? AND state=?",
                                 (state, container_id if container_id is not None else row["container_id"],
                                  canonical(result) if result is not None else row["result"], row["execution_id"], row["revision"], row["state"]))
            if updated.rowcount != 1:
                raise Unavailable()
        return self.get(UUID(row["execution_id"]))
