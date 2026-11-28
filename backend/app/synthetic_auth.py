"""隔離MVP専用の認証。通常入口からimport・選択・fallbackしない。

パスワードはPBKDF2、sessionはランダムなopaque tokenのdigestだけを保存する。
SQLite台帳の欠落・破損・書込み異常は拒否し、利用者指定UIDを認証に使わない。
"""
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from app.submission_ledger import ClosingConnection,private_storage,private_parent


class SyntheticAuthDenied(Exception):pass


def password_record(password):
    # saltはアカウントごとに生成する。平文passwordはfixture発行時のprivateファイルだけに
    # 保持し、このレコード・台帳・ログへは保存しない。通常Firebase用の鍵とは共用しない。
    if not isinstance(password,str) or not 20<=len(password)<=256:raise SyntheticAuthDenied()
    salt=secrets.token_bytes(16)
    return {'salt':base64.b64encode(salt).decode(),'digest':hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600000).hex()}


class SyntheticAuth:
    def __init__(self,path,accounts,*,guard=private_storage,clock=time.time):
        if set(accounts)!={'A','B'}:raise SyntheticAuthDenied()
        seen=set()
        for value in accounts.values():
            if set(value)!={'firebase_uid','email','name','salt','digest'}:raise SyntheticAuthDenied()
            if not re.fullmatch(r'stage10-synthetic-[a-z0-9-]{1,48}',value['firebase_uid']):raise SyntheticAuthDenied()
            if not value['email'].endswith('@example.com') or not isinstance(value['name'],str):raise SyntheticAuthDenied()
            salt=base64.b64decode(value['salt'],validate=True)
            if len(salt)!=16 or not re.fullmatch('[0-9a-f]{64}',value['digest']):raise SyntheticAuthDenied()
            if value['firebase_uid'] in seen:raise SyntheticAuthDenied()
            seen.add(value['firebase_uid'])
        if accounts['A']['digest']==accounts['B']['digest']:raise SyntheticAuthDenied()
        self.path=Path(path).absolute();self.accounts=copy.deepcopy(accounts);self.guard=guard;self.clock=clock
        self.identity=guard(self.path)
        with self.connection() as db:
            if db.execute('SELECT version FROM meta').fetchall()!=[(1,)]:raise SyntheticAuthDenied()

    @classmethod
    def provision(cls,path,accounts,*,guard=private_storage,clock=time.time):
        path=Path(path).absolute()
        if guard is private_storage:private_parent(path)
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        with sqlite3.connect(path,factory=ClosingConnection) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''CREATE TABLE meta(version INTEGER NOT NULL); INSERT INTO meta VALUES(1);
CREATE TABLE sessions(digest TEXT PRIMARY KEY, account TEXT NOT NULL, issued INTEGER NOT NULL, expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE bindings(editing_id TEXT PRIMARY KEY, digest TEXT NOT NULL REFERENCES sessions(digest));
CREATE TABLE attempts(account TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until INTEGER NOT NULL);''')
        return cls(path,accounts,guard=guard,clock=clock)

    def connection(self):
        # 欠落を新規DBと誤認しないようmode=rwで開く。inodeの置換・破損も拒否し、
        # 認証障害から「匿名でも許可」へfallbackする経路は作らない。
        if self.guard(self.path)!=self.identity:raise SyntheticAuthDenied()
        db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=1,factory=ClosingConnection)
        try:
            db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA synchronous=FULL')
            if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise SyntheticAuthDenied()
            return db
        except Exception:
            db.close();raise SyntheticAuthDenied() from None

    @staticmethod
    def digest(token):
        if not isinstance(token,str) or not re.fullmatch('[A-Za-z0-9_-]{43}',token):raise SyntheticAuthDenied()
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    def login(self,account,password):
        if account not in self.accounts or not isinstance(password,str) or len(password)>256:raise SyntheticAuthDenied()
        now=int(self.clock());record=self.accounts[account];success=False
        with self.connection() as db:
            # 試行回数の判定と更新を直列化する。再起動しても連続失敗を忘れず、
            # 別接続から同時に試して制限をすり抜けることを防ぐ。
            db.execute('BEGIN IMMEDIATE')
            attempt=db.execute('SELECT failures,blocked_until FROM attempts WHERE account=?',(account,)).fetchone()
            if attempt and attempt[1]>now:raise SyntheticAuthDenied()
            failures=attempt[0] if attempt and attempt[1]==0 else 0
            expected=hashlib.pbkdf2_hmac('sha256',password.encode(),base64.b64decode(record['salt']),600000).hex()
            success=hmac.compare_digest(expected,record['digest'])
            failures=0 if success else failures+1
            db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?)',(account,failures,now+30 if failures>=5 else 0))
            if success:
                # browserへ返す値は十分な乱数から作る。DBにはdigestだけを置き、
                # A/Bの選択ラベルやクライアント指定UIDをsession tokenにしない。
                token=secrets.token_urlsafe(32);expires=now+900
                db.execute('INSERT INTO sessions(digest,account,issued,expires) VALUES(?,?,?,?)',(self.digest(token),account,now,expires))
            db.commit()
        if not success:raise SyntheticAuthDenied()
        return {'token':token,'expires_at':expires,'environment':'SYNTHETIC_ONLY'}

    def _lookup(self,db,digest):
        row=db.execute('SELECT account,issued,expires,revoked FROM sessions WHERE digest=?',(digest,)).fetchone()
        now=int(self.clock())
        if row is None or row[0] not in self.accounts or row[3] or not row[1]<=now<row[2]:raise SyntheticAuthDenied()
        return {**self.accounts[row[0]],'account':row[0],'expires_at':row[2],'session_digest':digest}

    def authenticate(self,token):
        with self.connection() as db:return self._lookup(db,self.digest(token))

    def revoke(self,token):
        with self.connection() as db:
            digest=self.digest(token);self._lookup(db,digest)
            db.execute('UPDATE sessions SET revoked=1 WHERE digest=?',(digest,));db.commit()

    def bind(self,editing_id,login_digest,uid):
        """編集session発行時に一度だけ結合する。別ログインへの付替えは許可しない。"""
        from uuid import UUID
        if str(UUID(editing_id))!=editing_id:raise SyntheticAuthDenied()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE');current=self._lookup(db,login_digest)
            if current['firebase_uid']!=uid:raise SyntheticAuthDenied()
            db.execute('INSERT INTO bindings VALUES(?,?)',(editing_id,login_digest));db.commit()
            return current['expires_at']

    def verify_binding(self,editing_id,uid):
        """gateと収集認可が共有する読取り照会。失効済みログインからの接続を拒否する。"""
        with self.connection() as db:
            row=db.execute('SELECT digest FROM bindings WHERE editing_id=?',(editing_id,)).fetchone()
            if row is None:raise SyntheticAuthDenied()
            current=self._lookup(db,row[0])
            if current['firebase_uid']!=uid:raise SyntheticAuthDenied()
            return current['expires_at']
