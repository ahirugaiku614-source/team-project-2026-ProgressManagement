"""worker専用SQLiteで全体4枠と操作履歴を永続化する。既存PostgreSQLは使わない。"""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import sqlite3
import stat
import shutil
import time
from uuid import UUID

from execution_worker.contracts import Busy, Denied, LockBusy, Unavailable, canonical, fingerprint, full_id


# 無制限に証跡を削る代わりに、新規受付を止める保守的な上限。
# HTTP入力や環境変数で変更させない。同一台帳の全workerを同じ版で運用する。
MAX_JOBS = 10000
MAX_NONCES = 100000
MAX_LOCK_FILES = 10000
MAX_STATE_BYTES = 512 * 1024**2
MIN_FREE_BYTES = 64 * 1024**2


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
        self.get(execution_id)  # 未登録IDの復旧要求でlockファイルを増やさない。
        path = self.path.with_name(self.path.name + "." + str(execution_id) + ".lock")
        with self._file_lock(path):
            yield

    @contextmanager
    def admission_lock(self):
        """未完了検査→予約→実行lock取得を直列化し、予約直後を孤立と誤認させない。"""
        path = self.path.with_name(self.path.name + ".admission.lock")
        with self._file_lock(path, wait_ms=1000):
            yield

    @contextmanager
    def _file_lock(self, lock_path, wait_ms=0):
        """新規専用lockのみ扱う。競合はLockBusyに区別し、inodeをunlinkで交換しない。"""
        fd = None
        acquired = False
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Unavailable()
            if os.name == "nt":
                import msvcrt
                # Windowsのbyte lock用。新規専用lockファイルだけに1 byteを置く。
                if info.st_size == 0:
                    os.write(fd, b"0")
            else:
                import fcntl
            deadline = time.monotonic() + wait_ms / 1000
            while not acquired:
                try:
                    if os.name == "nt":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockBusy() from None
                    time.sleep(0.005)  # admissionの短い競合だけを待つ。実行lockは即時拒否。
            yield
        except (Busy, Denied, LockBusy):
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
                self._check_capacity(db, new_job=False)
                return dict(previous), False
            self._check_capacity(db, new_job=True)
            if (db.execute("SELECT 1 FROM jobs WHERE state='UNKNOWN'").fetchone()
                    or db.execute("SELECT count(*) FROM jobs WHERE state!='COMPLETED'").fetchone()[0] >= 4):
                raise Busy()
            db.execute("INSERT INTO jobs VALUES(?,?,?,?, 'RESERVED',NULL,1,NULL,?)",
                       (key, owner, request_hash, canonical(spec), now))
            return dict(db.execute("SELECT * FROM jobs WHERE execution_id=?", (key,)).fetchone()), True

    def unfinished(self):
        """再起動時に調べる非完了IDを返す。UNKNOWNも含め、証跡は削除しない。"""
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs WHERE state!='COMPLETED' ORDER BY execution_id")]

    def _usage(self, db):
        """件数・DB/sidecar bytes・実行lock数を観測する。ファイル名や内容は報告しない。"""
        usage = {"jobs": db.execute("SELECT count(*) FROM jobs").fetchone()[0],
                 "nonces": db.execute("SELECT count(*) FROM nonces").fetchone()[0],
                 "locks": 0, "orphan_locks": 0, "state_bytes": 0,
                 "free_bytes": shutil.disk_usage(self.path.parent).free}
        for suffix in ("", "-journal", "-wal", "-shm", ".admission.lock"):
            try:
                info = Path(str(self.path) + suffix).lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Unavailable()
            usage["state_bytes"] += info.st_size
        prefix = self.path.name + "."
        with os.scandir(self.path.parent) as entries:
            for entry in entries:
                if not entry.name.startswith(prefix) or not entry.name.endswith(".lock") or entry.name == self.path.name + ".admission.lock":
                    continue
                key = entry.name[len(prefix):-5]
                if str(UUID(key)) != key:
                    raise Unavailable()
                # WindowsのDirEntry.statではnlinkが0のことがあるため、lstatを直接使う。
                info = Path(entry.path).lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise Unavailable()
                usage["locks"] += 1
                usage["state_bytes"] += info.st_size
                if not db.execute("SELECT 1 FROM jobs WHERE execution_id=?", (key,)).fetchone():
                    usage["orphan_locks"] += 1
                if usage["locks"] > MAX_LOCK_FILES:
                    break  # 過大なdirectoryを無限走査して受付を占有しない。
        return usage

    def _check_capacity(self, db, new_job):
        """nonce/新規row書込み前に上限を検査する。超過時はtransactionごとrollbackする。"""
        usage = self._usage(db)
        # admitはこのtransactionですでにnonceを1件挿入している。超過ならrollbackされる。
        if (usage["nonces"] > MAX_NONCES or usage["jobs"] + int(new_job) > MAX_JOBS
                or usage["locks"] + int(new_job) > MAX_LOCK_FILES or usage["orphan_locks"]
                or usage["state_bytes"] >= MAX_STATE_BYTES or usage["free_bytes"] < MIN_FREE_BYTES):
            raise Busy()

    def capacity_report(self):
        """管理者向け容量観測だけを行う。UNKNOWNやnonce/lockを間引かない。"""
        with self.connection() as db:
            return self._usage(db)

    def redact_completed(self, before_created_ms, maximum=100):
        """明示保守で古い完了結果だけを秘匿する。ID/hash/nonce/未完了証跡/lockは保持する。

        tombstoneが残るため同じ実行IDを再実行しない。SQLite物理ファイルは自動縮小しない。
        稼働中のlockを消す方式は別processの排他を分断するので採用しない。
        """
        if type(before_created_ms) is not int or before_created_ms < 0 or type(maximum) is not int or not 1 <= maximum <= 1000:
            raise Unavailable()
        changed = 0
        with self.admission_lock(), self.transaction() as db:
            rows = db.execute("SELECT execution_id FROM jobs WHERE state='COMPLETED' AND result IS NOT NULL AND created_ms<=? ORDER BY created_ms LIMIT ?",
                              (before_created_ms, maximum)).fetchall()
            for row in rows:
                try:
                    with self.job_lock(UUID(row["execution_id"])):
                        changed += db.execute("UPDATE jobs SET result=NULL,revision=revision+1 WHERE execution_id=? AND state='COMPLETED' AND result IS NOT NULL",
                                              (row["execution_id"],)).rowcount
                except LockBusy:
                    continue
        return changed

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
