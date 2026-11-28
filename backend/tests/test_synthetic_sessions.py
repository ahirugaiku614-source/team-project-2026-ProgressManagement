"""製品の編集セッション・現在認可と合成ログイン台帳を接続して検証する。"""
from pathlib import Path
import tempfile
import unittest
from app import models
from app.submission_scope import SubmissionTarget
from app.submission_provider import ProviderUnavailable
from app.synthetic_auth import SyntheticAuth,password_record
from app.synthetic_application import bound_sessions
from test_synthetic_auth import guard
import test_work_environment as fixtures


class BoundSessionTests(unittest.TestCase):
    setUpClass=fixtures.EnvironmentTests.__dict__['setUpClass']
    tearDownClass=fixtures.EnvironmentTests.__dict__['tearDownClass']
    tearDown=fixtures.EnvironmentTests.tearDown
    headers=staticmethod(fixtures.EnvironmentTests.headers)
    decode_token=staticmethod(fixtures.EnvironmentTests.decode_token)
    register=fixtures.EnvironmentTests.register
    start=fixtures.EnvironmentTests.start
    request=fixtures.EnvironmentTests.request

    def setUp(self):
        fixtures.EnvironmentTests.setUp(self)
        self.basis=self.start()
        self.member.firebase_uid='stage10-synthetic-a';self.member.email='a@example.com';self.db.commit()
        accounts={label:{'firebase_uid':'stage10-synthetic-'+label.lower(),'email':label.lower()+'@example.com',
            'name':'合成'+label,**password_record('synthetic-session-'+label+'-password-123')} for label in ('A','B')}
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.auth=SyntheticAuth.provision(Path(self.directory.name)/'auth.sqlite',accounts,guard=guard)
        self.token=self.auth.login('A','synthetic-session-A-password-123')['token']
        self.sessions=bound_sessions(self.db,self.auth,self.auth.digest(self.token))
        b=self.basis
        self.target=SubmissionTarget(b.team_id,b.project_id,b.task_id,b.member_id,b.folder_path)

    def open(self):return self.sessions.open(self.member,self.basis)['session_id']

    def test_product_session_is_bound(self):
        sid=self.open();evidence=self.sessions.verify(self.target,sid,self.basis.source_generation)
        self.assertEqual(evidence.session_id,sid)
        self.assertEqual(evidence.workspace_id,self.basis.workspace_id)

    def test_logout_invalidates_cookie_gate_verifier(self):
        sid=self.open();self.auth.revoke(self.token)
        with self.assertRaises(ProviderUnavailable):bound_sessions(self.db,self.auth).verify(self.target,sid,self.basis.source_generation)

    def test_wrong_login_cannot_open(self):
        token=self.auth.login('B','synthetic-session-B-password-123')['token']
        with self.assertRaises(ProviderUnavailable):bound_sessions(self.db,self.auth,self.auth.digest(token)).open(self.member,self.basis)

    def test_db_revocation_still_checked(self):
        sid=self.open();self.db.get(models.WorkEditingSession,sid).revoked_at=__import__('datetime').datetime.utcnow();self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.sessions.verify(self.target,sid,self.basis.source_generation)

    def test_unbound_session_is_not_authenticated(self):
        from app.work_environment import SnapshotEditingSessions
        sid=SnapshotEditingSessions(self.db).open(self.member,self.basis)['session_id']
        with self.assertRaises(ProviderUnavailable):self.sessions.verify(self.target,sid,self.basis.source_generation)


if __name__=='__main__':unittest.main()
