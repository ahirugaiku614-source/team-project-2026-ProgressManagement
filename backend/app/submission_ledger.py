"""収集専用の永続単発受付台帳。本文・鍵・許可原文は保存しない。"""
import os
from pathlib import Path
import sqlite3
import stat
import sys
from uuid import UUID
from app.submission_rpc import reject


class ClosingConnection(sqlite3.Connection):
    # SQLiteの標準context managerは接続を閉じないため、明示的に解放する。
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()


def private_parent(path):
    # 実環境は専用所有者の0700ディレクトリ・0600普通ファイルに限定する。
    # Windows試験はこの検査だけを偽物に置換し、OS権限保証とは分ける。
    if sys.platform != 'linux': reject()
    path = Path(path)
    for parent in path.parents:
        s = parent.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid not in (0, os.geteuid()) or s.st_mode & 0o022: reject()
    parent = path.parent.stat()
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700: reject()


def private_storage(path):
    path=Path(path)
    private_parent(path)
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o600: reject()
    return s.st_dev, s.st_ino


class SQLiteReplayLedger:
    """新規初期化は明示provisionのみ。再起動時の欠落を新規DBとして扱わない。

    原子的INSERTを収集前にFULL同期commitする。失敗した要求も削除・再予約しない。
    台帳全体の整合した巻戻しは独立した基準なしでは検出できない。
    """
    def __init__(self, path, *, guard=private_storage):
        self.path = Path(path).absolute(); self.guard = guard; self.failed = False
        try:
            self.identity = guard(self.path)
            with self._connect() as db: self._check(db)
        except Exception:
            self.failed = True; reject()

    @classmethod
    def provision(cls, path, *, guard=private_storage):
        # 既存ファイルは上書きしない。初期化失敗の残骸も自動削除しない。
        path = Path(path).absolute()
        if guard is private_storage: private_parent(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        guard(path)
        with sqlite3.connect(path, factory=ClosingConnection) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript("CREATE TABLE meta(version INTEGER NOT NULL CHECK(version=1)); INSERT INTO meta VALUES(1); CREATE TABLE requests(submission_id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('ACCEPTED','COMPLETE','UNKNOWN')));")
        # ファイル名自体の永続化はLinux親ディレクトリの同期で確認する。
        if sys.platform == 'linux':
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(parent)
            finally: os.close(parent)
        return cls(path, guard=guard)

    def _connect(self):
        if self.failed or self.guard(self.path) != self.identity: reject()
        # mode=rwにより、消えた台帳をSQLiteが勝手に新規作成することを防ぐ。
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=1, factory=ClosingConnection)
        try:
            db.execute('PRAGMA synchronous=FULL'); db.execute('PRAGMA busy_timeout=1000')
        except Exception:
            db.close(); raise
        return db

    def _check(self, db):
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]: reject()
        if db.execute('SELECT version FROM meta').fetchall() != [(1,)]: reject()
        if db.execute("SELECT count(*) FROM requests WHERE state NOT IN ('ACCEPTED','COMPLETE','UNKNOWN')").fetchone()[0]: reject()

    def claim(self, submission_id):
        try:
            if str(UUID(submission_id)) != submission_id: reject()
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE'); self._check(db)
                db.execute('INSERT INTO requests VALUES (?,?)', (submission_id, 'ACCEPTED'))
                db.commit()  # このcommit成功まで収集を開始しない。
        except sqlite3.IntegrityError:
            reject()  # 重複は拒否するが、正常な台帳全体を壊れた扱いにはしない。
        except Exception:
            self.failed = True; reject()

    def finish(self, submission_id, state):
        try:
            if state not in ('COMPLETE', 'UNKNOWN'): reject()
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE'); self._check(db)
                if db.execute('UPDATE requests SET state=? WHERE submission_id=? AND state=?',
                              (state, submission_id, 'ACCEPTED')).rowcount != 1: reject()
                db.commit()
        except Exception:
            self.failed = True; reject()
