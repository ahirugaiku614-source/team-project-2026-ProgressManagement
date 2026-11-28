"""合成認証を実SQLite台帳で検証する。Windowsでは所有者検査だけを明示的に置換する。"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from app.synthetic_auth import SyntheticAuth, SyntheticAuthDenied, password_record


def guard(path):
    # WindowsのACLをLinux mode保証と取り違えない。台帳の実処理は置換しない。
    value=Path(path).stat()
    return value.st_dev,value.st_ino


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.passwords={'A':'synthetic-only-A-password-123','B':'synthetic-only-B-password-456'}
        cls.accounts={label:{'firebase_uid':'stage10-synthetic-'+label.lower(),
            'email':label.lower()+'@example.com','name':'合成'+label,
            **password_record(password)} for label,password in cls.passwords.items()}

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'auth.sqlite'
        self.now=1700000000
        self.auth=SyntheticAuth.provision(self.path,self.accounts,guard=guard,clock=lambda:self.now)

    def login(self,label='A'):
        return self.auth.login(label,self.passwords[label])['token']

    def test_separate_accounts(self):
        for label in ('A','B'):
            self.assertEqual(self.auth.authenticate(self.login(label))['account'],label)

    def test_wrong_password(self):
        with self.assertRaises(SyntheticAuthDenied):self.auth.login('B',self.passwords['A'])

    def test_uid_is_not_credential(self):
        with self.assertRaises(SyntheticAuthDenied):self.auth.login('stage10-synthetic-a',self.passwords['A'])

    def test_unknown_token(self):
        with self.assertRaises(SyntheticAuthDenied):self.auth.authenticate('a'*43)

    def test_expiration(self):
        token=self.login();self.now+=900
        with self.assertRaises(SyntheticAuthDenied):self.auth.authenticate(token)

    def test_revocation_survives_reopen(self):
        token=self.login();self.auth.revoke(token)
        reopened=SyntheticAuth(self.path,self.accounts,guard=guard,clock=lambda:self.now)
        with self.assertRaises(SyntheticAuthDenied):reopened.authenticate(token)

    def test_binding_revoked_with_login(self):
        token=self.login();editing=str(uuid4());uid=self.accounts['A']['firebase_uid']
        self.auth.bind(editing,self.auth.digest(token),uid)
        self.auth.verify_binding(editing,uid)
        self.auth.revoke(token)
        with self.assertRaises(SyntheticAuthDenied):self.auth.verify_binding(editing,uid)

    def test_other_account_cannot_bind(self):
        with self.assertRaises(SyntheticAuthDenied):
            self.auth.bind(str(uuid4()),self.auth.digest(self.login()),self.accounts['B']['firebase_uid'])

    def test_unknown_binding(self):
        with self.assertRaises(SyntheticAuthDenied):self.auth.verify_binding(str(uuid4()),self.accounts['A']['firebase_uid'])

    def test_persistent_rate_limit(self):
        for _ in range(5):
            with self.assertRaises(SyntheticAuthDenied):self.auth.login('A','wrong-password')
        with self.assertRaises(SyntheticAuthDenied):self.login()
        self.now+=31
        self.login()

    def test_no_plaintext_session_or_password_in_database(self):
        token=self.login()
        db=sqlite3.connect(self.path)
        try:dump='\n'.join(db.iterdump())
        finally:db.close()
        self.assertNotIn(token,dump)
        for value in self.passwords.values():self.assertNotIn(value,dump)

    def test_no_overwrite(self):
        with self.assertRaises(FileExistsError):SyntheticAuth.provision(self.path,self.accounts,guard=guard)

    def test_corruption_refused(self):
        self.path.write_bytes(b'not a database')
        with self.assertRaises(Exception):self.login()

    def test_shared_routes_do_not_import_firebase(self):
        # 新しいprocessで確認する。他suiteが読み込んだFirebase moduleの影響を受けない。
        code="import sys;import app.application_routes;assert 'app.firebase' not in sys.modules;assert 'firebase_admin' not in sys.modules"
        result=subprocess.run([sys.executable,'-B','-c',code],env={**os.environ,'DATABASE_URL':'sqlite://'},capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0)

    def test_synthetic_entry_refuses_without_explicit_approval(self):
        result=subprocess.run([sys.executable,'-B','-c','import app.synthetic_asgi'],
            env={**os.environ,'STAGE10_SYNTHETIC_APPROVED':'0','DATABASE_URL':'sqlite://'},capture_output=True,timeout=30)
        self.assertNotEqual(result.returncode,0)


if __name__=='__main__':unittest.main()
