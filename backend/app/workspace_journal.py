"""初期化受付の永続台帳。本文を保存せず、失敗・不明の受付は再利用しない。"""
import json
import os
from pathlib import Path
import sqlite3
from app.submission_ledger import private_storage,private_parent,ClosingConnection
from app.submission_rpc import reject


class WorkspaceJournal:
    def __init__(self,path,*,guard=private_storage):
        self.path=Path(path).absolute();self.guard=guard;self.identity=guard(self.path)
        with self.connect() as db:
            if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:reject()
            if db.execute('SELECT version FROM meta').fetchall()!=[(1,)]:reject()

    @classmethod
    def provision(cls,path,*,guard=private_storage):
        path=Path(path).absolute()
        if guard is private_storage:private_parent(path)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        with sqlite3.connect(path,factory=ClosingConnection) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript("CREATE TABLE meta(version INTEGER NOT NULL CHECK(version=1)); INSERT INTO meta VALUES(1); CREATE TABLE workspaces(basis TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('ACCEPTED','COMPLETE','UNKNOWN')), proof TEXT);")
        if os.name=='posix':
            fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        return cls(path,guard=guard)

    def connect(self):
        if self.guard(self.path)!=self.identity:reject()
        db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=1,factory=ClosingConnection)
        db.execute('PRAGMA synchronous=FULL');db.execute('PRAGMA busy_timeout=1000')
        return db

    def lookup(self,basis):
        with self.connect() as db:
            if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:reject()
            row=db.execute('SELECT fingerprint,state,proof FROM workspaces WHERE basis=?',(basis,)).fetchone()
        if row is None:return None
        if row[1] not in ('ACCEPTED','COMPLETE','UNKNOWN'):reject()
        return {'fingerprint':row[0],'state':row[1],'proof':json.loads(row[2]) if row[2] else None}

    def claim(self,basis,fingerprint):
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO workspaces VALUES (?,?,?,NULL)',(basis,fingerprint,'ACCEPTED'))
                db.commit()  # この同期受付が成功するまではワーカーを起動しない。
        except Exception:reject()

    def finish(self,basis,fingerprint,proof=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute('UPDATE workspaces SET state=?,proof=? WHERE basis=? AND fingerprint=? AND state=?',
                ('COMPLETE' if proof is not None else 'UNKNOWN',json.dumps(proof,sort_keys=True) if proof else None,
                 basis,fingerprint,'ACCEPTED')).rowcount
            if changed!=1:reject()
            db.commit()
