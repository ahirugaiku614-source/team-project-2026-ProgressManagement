"""専用一時ファイル用SQLiteレジストリ。本番DBの方式を決定・移行するものではない。

各操作はBEGIN IMMEDIATEで枠計数と予約を一つの書込みトランザクションにする。
同じローカルファイルを使う複数プロセスで共有できる。NFS/複数ホストは非対応。
既存ファイルは新規作成で上書きせず、再開時も専用marker以外のDBを受け付けない。
"""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat

from vm_manager.contracts import ClockRegression, RegistryUnavailable

APPLICATION_ID = 0x564D4D39
DDL = (
    "CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1), marker TEXT NOT NULL, max_running INTEGER NOT NULL CHECK(max_running BETWEEN 1 AND 4), last_ms INTEGER NOT NULL)",
    """CREATE TABLE environments (
      project_id INTEGER NOT NULL, user_id INTEGER NOT NULL, team_id INTEGER NOT NULL,
      vm_id TEXT NOT NULL UNIQUE, disk_id TEXT NOT NULL UNIQUE,
      state TEXT NOT NULL CHECK(state IN ('ABSENT','PREPARING','STARTING','RUNNING','STOPPING','STOPPED','QUARANTINED')),
      generation INTEGER NOT NULL CHECK(generation>=0), active_op TEXT,
      lease_until_ms INTEGER, vm_seen INTEGER NOT NULL CHECK(vm_seen IN (0,1)),
      disk_seen INTEGER NOT NULL CHECK(disk_seen IN (0,1)), error_code TEXT,
      PRIMARY KEY(project_id,user_id),
      CHECK ((active_op IS NULL AND lease_until_ms IS NULL) OR (active_op IS NOT NULL AND lease_until_ms IS NOT NULL))
    )""",
    """CREATE TABLE operations (
      operation_id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, user_id INTEGER NOT NULL, team_id INTEGER NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('ENSURE_STARTED','STOP','GET_STATE')),
      generation INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('ACTIVE','SUCCEEDED','UNKNOWN')),
      result_state TEXT NOT NULL, error_code TEXT,
      FOREIGN KEY(project_id,user_id) REFERENCES environments(project_id,user_id) ON DELETE RESTRICT
    )""",
)


class SQLiteRegistry:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self._check_file()
        try:
            with self.connection() as db:
                if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                    raise RegistryUnavailable("not_a_reference_registry")
                row = db.execute("SELECT marker FROM meta WHERE id=1").fetchone()
                if row is None or row[0] != "vm-manager-test-reference-v1":
                    raise RegistryUnavailable("not_a_reference_registry")
        except sqlite3.Error:
            raise RegistryUnavailable("registry_unavailable") from None

    @classmethod
    def create_new(cls, path, max_running=4):
        if type(max_running) is not int or not 1 <= max_running <= 4:
            raise ValueError("invalid_reference_limit")
        path = Path(path).absolute()
        # 呼出し元が用意した私有の試験ディレクトリだけを使う。URIや接続文字列は受け付けない。
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        try:
            db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                for statement in DDL:
                    db.execute(statement)
                db.execute("INSERT INTO meta VALUES(1,?,?,0)", ("vm-manager-test-reference-v1", max_running))
                db.commit()
            finally:
                db.close()
        except sqlite3.Error:
            # 初期化途中の新規ファイルも勝手に消さない。再作成で隠さず検査対象として残す。
            raise RegistryUnavailable("registry_initialization_failed") from None
        return cls(path)

    def _check_file(self):
        try:
            info = self.path.lstat()
        except OSError:
            raise RegistryUnavailable("registry_file_unavailable") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RegistryUnavailable("unsafe_registry_file")
        return info.st_dev, info.st_ino

    @contextmanager
    def connection(self):
        """接続の前後に同じ通常ファイルであることを確認し、終了時には必ずcloseする。"""
        before = self._check_file()
        db = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        try:
            if self._check_file() != before:
                raise RegistryUnavailable("registry_file_changed")
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, now_ms):
        """全プロセス共通の書込みロックで枠を予約し、例外なら操作意図ごと取り消す。

        ロック待ちの失敗や時計逆行時には副作用を許可しない。DBエラーの詳細も隠す。
        外部ドライバーはこのトランザクションに含めず、確定した意図を基点に復旧する。
        """
        if type(now_ms) is not int or not 0 < now_ms < 2**62:
            raise ValueError("invalid_clock")
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    previous = db.execute("SELECT last_ms FROM meta WHERE id=1").fetchone()[0]
                    if now_ms < previous:
                        raise ClockRegression("clock_moved_backwards")
                    db.execute("UPDATE meta SET last_ms=? WHERE id=1", (now_ms,))
                    yield db
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        except sqlite3.Error:
            raise RegistryUnavailable("registry_transaction_failed") from None

    def snapshot(self):
        """テスト・診断用の読み取り。実ユーザーデータのファイルは保持していない。"""
        with self.connection() as db:
            return {table: [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY 1,2")]
                    for table in ("environments", "operations")}
