"""RPC factory・製品Git store・提出DB処理の接続回帰。

Firebase、通信、FDの実環境には接続しない。通信だけをインメモリで結び、
認可発行／検証、単発受付、Git証拠照合、DB状態遷移は製品コードを使用する。
GitのOSアダプターは既存の隔離フィクスチャであり、Linuxの実証ではない。
"""
from dataclasses import replace
from datetime import datetime
from uuid import uuid4
import tempfile
import time
import unittest
from unittest.mock import patch

import test_submissions as fixtures
from submission_snapshot_fakes import PortableGitFixture
from app import models
from app.submission_authorization import (
    authorized_rpc_provider, CollectorAuthorizationVerifier, VerifiedSession,
)
from app.submission_git import GitSnapshotStore
from app.submission_provider import ProviderUnavailable, SubmissionCollection
from app.submission_rpc import CollectorRPCServer, MemoryReplayLedger, RPCKeys
from app.submission_scope import SubmissionTarget
from app.submissions import reconcile_submission


class SavedSessionFixture:
    """合成セッションのみ。実code-serverの保存状態を証明するものではない。"""
    def __init__(self, evidence):
        self.evidence = evidence

    def verify(self, *args):
        return self.evidence


class InMemoryTransport:
    """実socketを開かず、製品RPC処理器へ渡す。要求本文や鍵は記録しない。"""
    def __init__(self, server):
        self.server = server
        self.calls = 0
        self.disconnect = None

    def exchange(self, frame, **kwargs):
        self.calls += 1
        if self.disconnect == 'before':
            raise ConnectionError('synthetic disconnect')
        response = self.server.handle(frame)
        if self.disconnect == 'after':
            raise ConnectionError('synthetic disconnect')
        return response


class RPCProviderConnectionTests(unittest.TestCase):
    # 既存の認証付きAPI・SQLite合成DBの準備だけを再利用する。
    # TestCaseを継承コピーしないため、既存試験を二重に発見しない。
    setUpClass = fixtures.SubmissionAPITests.__dict__['setUpClass']
    tearDownClass = fixtures.SubmissionAPITests.__dict__['tearDownClass']
    tearDown = fixtures.SubmissionAPITests.tearDown
    headers = staticmethod(fixtures.SubmissionAPITests.headers)
    decode_token = staticmethod(fixtures.SubmissionAPITests.decode_token)
    register = fixtures.SubmissionAPITests.register
    post = fixtures.SubmissionAPITests.post
    permit = fixtures.SubmissionAPITests.permit
    payload = fixtures.SubmissionAPITests.payload

    def setUp(self):
        fixtures.SubmissionAPITests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.git = PortableGitFixture(self.temp.name)
        self.store = GitSnapshotStore(self.git.session)
        self.now = int(time.time())
        self.target = SubmissionTarget(self.team.id, self.project.id, self.task.id,
                                       self.member.id, 'assignments/task-1')
        self.sessions = SavedSessionFixture(VerifiedSession(
            self.target, self.session, 1, self.now + 3600, True))
        # 公開された合成鍵。実環境の値は使用せず、方向別・用途別にも分離する。
        self.rpc_keys = RPCKeys(b'synthetic-request-key' * 2, b'synthetic-response-key' * 2)
        self.authorization_key = b'synthetic-provider-authorization-key'
        self.collection_states = []
        self.files = {'main.txt': ('100644', b'synthetic saved file\n')}

        def collect(target, session_id):
            row = self.db.query(models.TaskSubmission).one()
            self.collection_states.append(row.state)
            self.assertEqual(target, self.target)
            self.assertEqual(session_id, self.session)
            return dict(self.files)

        verifier = CollectorAuthorizationVerifier(self.authorization_key, 'collector-a',
            self.target, 1, sessions=self.sessions, clock=lambda: self.now)
        self.server = CollectorRPCServer(self.rpc_keys, MemoryReplayLedger(),
            {self.target: (1, collect)}, verifier, clock=lambda: self.now)
        self.transport = InMemoryTransport(self.server)
        self.provider = self.make_provider(self.member)
        from app.submission_provider import get_submission_provider
        self.main.app.dependency_overrides[get_submission_provider] = lambda: self.provider

    def make_provider(self, actor):
        return authorized_rpc_provider(self.db, actor, rpc_keys=self.rpc_keys,
            authorization_key=self.authorization_key, collector_id='collector-a',
            target=self.target, generation=1, store=self.store, transport=self.transport,
            sessions=self.sessions, ledger=MemoryReplayLedger(), clock=lambda: self.now)

    def saved(self):
        body = self.payload()
        response = self.post('/submissions', body)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['state'], 'SUBMITTED')
        row = self.db.query(models.TaskSubmission).one()
        request = SubmissionCollection(row.id, row.request_id, row.editing_session_id, self.target)
        return body, row, request, self.store.recover(request)

    def assert_one_collection(self):
        self.assertEqual(self.collection_states, ['PROCESSING'])
        self.assertEqual(self.transport.calls, 1)

    def test_processing_collects_through_real_factory_store_and_database(self):
        _, row, request, receipt = self.saved()
        self.assertEqual(receipt.submission_id, row.id)
        self.assertEqual(self.git.submission_ref(row.id), row.commit_id)
        # このフィクスチャはFD相対read_refを実装しないため、独立したGit一覧で観測する。
        self.assertEqual(self.git.run('for-each-ref', '--format=%(objectname)', 'refs/heads/main'), b'')
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 0)
        self.provider.verify(request, receipt)
        self.assert_one_collection()

    def test_submitted_repeat_preserves_success_without_collection(self):
        body, row, _, _ = self.saved()
        original = (row.id, row.commit_id, row.git_ref)
        result = self.post('/submissions', body)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['state'], 'SUBMITTED')
        self.assertEqual((row.id, row.commit_id, row.git_ref), original)
        self.assert_one_collection()

    def test_historical_verification_does_not_require_live_permit_or_clean_session(self):
        body, row, request, receipt = self.saved()
        self.now += 7200
        self.sessions.evidence = replace(self.sessions.evidence, saved=False, expires_at=self.now - 1)
        self.provider.verify(request, receipt)
        self.assertEqual(self.post('/submissions', body).json()['state'], 'SUBMITTED')
        self.assertEqual(row.state, 'SUBMITTED')
        # 過去の読取りを許しても、同じ行の新規収集認可は発行しない。
        with self.assertRaises(ProviderUnavailable):
            self.provider.authorize(request)
        self.assert_one_collection()

    def test_database_failure_recovers_existing_evidence_after_expiry(self):
        body = self.payload()
        original = self.db.commit
        calls = []

        def commit():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('synthetic database failure')
            return original()

        with patch.object(self.db, 'commit', side_effect=commit):
            result = self.post('/submissions', body)
        self.assertEqual(result.json()['state'], 'REVIEW_REQUIRED')
        row = self.db.query(models.TaskSubmission).one()
        oid = self.git.submission_ref(row.id)
        self.assertIsNotNone(oid)
        self.now += 7200
        self.sessions.evidence = replace(self.sessions.evidence, saved=False, expires_at=self.now - 1)
        result = reconcile_submission(self.db, self.member, row.id, self.provider)
        self.assertEqual((result['state'], result['commit_id']), ('SUBMITTED', oid))
        self.assertEqual(self.post('/submissions', body).json()['state'], 'SUBMITTED')
        self.assert_one_collection()

    def test_processing_after_lost_database_publication_recovers_without_rpc(self):
        _, row, _, receipt = self.saved()
        # 最終DBトランザクションだけが失われた場合の保存済み受付を再現する。
        row.state = 'PROCESSING'
        row.git_ref = row.commit_id = row.file_count = row.total_bytes = row.submitted_at = None
        self.db.commit()
        result = reconcile_submission(self.db, self.member, row.id, self.provider)
        self.assertEqual(result['state'], 'SUBMITTED')
        self.assertEqual(result['commit_id'], receipt.commit_id)
        self.assert_one_collection()

    def test_submitted_explicit_reconciliation_is_read_only_for_git(self):
        _, row, _, receipt = self.saved()
        for _ in range(2):
            result = reconcile_submission(self.db, self.member, row.id, self.provider)
            self.assertEqual(result['state'], 'SUBMITTED')
            self.assertEqual(result['commit_id'], receipt.commit_id)
        self.assert_one_collection()

    def test_other_authenticated_member_cannot_verify_or_recover(self):
        _, _, request, receipt = self.saved()
        other = self.make_provider(self.owner)
        with self.assertRaises(ProviderUnavailable):other.verify(request, receipt)
        with self.assertRaises(ProviderUnavailable):other.recover(request)
        self.assert_one_collection()

    def test_other_member_project_task_and_folder_evidence_is_rejected(self):
        _, _, request, receipt = self.saved()
        for field in ('student_id', 'project_id', 'task_id'):
            target = replace(self.target, **{field: getattr(self.target, field) + 100})
            altered = replace(request, target=target)
            with self.subTest(field=field):
                with self.assertRaises(ProviderUnavailable):self.provider.verify(altered, receipt)
                with self.assertRaises(ProviderUnavailable):self.provider.recover(altered)
        with self.assertRaises(ProviderUnavailable):
            self.provider.recover(replace(request, target=replace(self.target, folder_path='another/task')))
        self.assert_one_collection()

    def test_submission_request_and_session_ids_cannot_be_transferred(self):
        _, _, request, receipt = self.saved()
        for field in ('submission_id', 'request_id', 'editing_session_id'):
            altered = replace(request, **{field: str(uuid4())})
            with self.subTest(field=field):
                with self.assertRaises(ProviderUnavailable):self.provider.verify(altered, receipt)
                with self.assertRaises(ProviderUnavailable):self.provider.recover(altered)
        self.assert_one_collection()

    def test_membership_revocation_still_refuses_historical_access(self):
        _, _, request, receipt = self.saved()
        self.db.query(models.TeamMember).filter_by(team_id=self.team.id, user_id=self.member.id).delete()
        self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.provider.verify(request, receipt)
        with self.assertRaises(ProviderUnavailable):self.provider.recover(request)
        self.assert_one_collection()

    def test_reassignment_still_refuses_historical_access(self):
        _, _, request, receipt = self.saved()
        self.task.assignee_id = self.owner.id
        self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.provider.verify(request, receipt)
        with self.assertRaises(ProviderUnavailable):self.provider.recover(request)
        self.assert_one_collection()

    def test_changed_registered_folder_refuses_historical_access(self):
        _, _, request, receipt = self.saved()
        self.db.query(models.TaskSubmissionTarget).one().folder_path = 'another/task'
        self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.provider.verify(request, receipt)
        with self.assertRaises(ProviderUnavailable):self.provider.recover(request)
        self.assert_one_collection()

    def test_consumption_and_digest_evidence_remain_required(self):
        _, row, request, receipt = self.saved()
        permit = self.db.get(models.SubmissionPermit, row.permit_id)
        consumed = permit.consumed_at
        permit.consumed_at = None
        self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.provider.verify(request, receipt)
        permit.consumed_at = consumed
        row.permit_digest = 'a' * 64
        self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.provider.recover(request)
        self.assert_one_collection()

    def test_different_receipt_is_not_accepted_for_same_submission(self):
        _, row, request, receipt = self.saved()
        with self.assertRaises(ProviderUnavailable):
            self.provider.verify(request, replace(receipt, commit_id='a' * 40))
        self.assertEqual(row.state, 'SUBMITTED')
        self.assert_one_collection()

    def test_missing_git_ref_is_not_recreated_or_confirmed(self):
        body, row, _, _ = self.saved()
        self.git.run('update-ref', '-d', row.git_ref)
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, row.id, self.provider)['state'], 'REVIEW_REQUIRED')
        self.assertIsNone(self.git.submission_ref(row.id))
        self.assert_one_collection()

    def test_corrupt_git_journal_is_not_success(self):
        body, row, _, _ = self.saved()
        (self.git.journal / (row.id + '.complete')).write_bytes(b'{invalid synthetic journal')
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, row.id, self.provider)['state'], 'REVIEW_REQUIRED')
        self.assert_one_collection()

    def test_replaced_git_snapshot_is_not_accepted(self):
        body, row, _, _ = self.saved()
        tree = self.git.tree({'main.txt': ('100644', b'different synthetic file')})
        changed = self.git.run('commit-tree', tree, data=b'synthetic replacement\n').decode().strip()
        self.git.run('update-ref', row.git_ref, changed)
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, row.id, self.provider)['state'], 'REVIEW_REQUIRED')
        self.assert_one_collection()

    def test_database_receipt_mismatch_is_not_confirmed(self):
        body, row, _, _ = self.saved()
        row.commit_id = 'a' * 40
        self.db.commit()
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, row.id, self.provider)['state'], 'REVIEW_REQUIRED')
        self.assert_one_collection()

    def test_disconnect_after_collection_never_collects_twice(self):
        body = self.payload()
        self.transport.disconnect = 'after'
        first = self.post('/submissions', body).json()
        self.assertEqual(first['state'], 'REVIEW_REQUIRED')
        self.transport.disconnect = None
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, first['id'], self.provider)['state'], 'REVIEW_REQUIRED')
        self.assert_one_collection()

    def test_git_failure_retains_admission_without_recollection(self):
        body = self.payload()
        self.git.failure = 'git'
        first = self.post('/submissions', body).json()
        self.assertEqual(first['state'], 'REVIEW_REQUIRED')
        self.git.failure = None
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, first['id'], self.provider)['state'], 'REVIEW_REQUIRED')
        self.assert_one_collection()

    def test_temporary_storage_verification_preserves_submitted(self):
        # 保存済み証拠を読めないことは、証拠が壊れたことの証明ではない。
        from app.project_storage import StorageUnavailable
        body, row, _, _ = self.saved()
        with patch.object(self.store, 'recover', side_effect=StorageUnavailable('synthetic unavailable')):
            self.assertEqual(self.post('/submissions', body).status_code, 503)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.TaskSubmission, row.id).state, 'SUBMITTED')
        self.assert_one_collection()

    def test_reconcile_ack_loss_preserves_committed_success(self):
        # commitを本当に実行した後で応答喪失を発生させ、実行前失敗と区別する。
        _, row, _, _ = self.saved()
        row.state = 'REVIEW_REQUIRED'
        self.db.commit()
        original = self.db.commit
        lost = []

        def commit():
            original()
            if not lost:
                lost.append(True)
                raise RuntimeError('synthetic acknowledgement loss')

        with patch.object(self.db, 'commit', side_effect=commit):
            result = reconcile_submission(self.db, self.member, row.id, self.provider)
        self.assertEqual(result['state'], 'SUBMITTED')
        self.db.expire_all()
        self.assertEqual(self.db.get(models.TaskSubmission, row.id).state, 'SUBMITTED')
        self.assert_one_collection()

    def test_database_ack_loss_preserves_committed_success(self):
        body = self.payload()
        original = self.db.commit
        calls = []

        def commit():
            calls.append(1)
            result = original()
            if len(calls) == 2:
                raise RuntimeError('synthetic acknowledgement loss')
            return result

        with patch.object(self.db, 'commit', side_effect=commit):
            first = self.post('/submissions', body).json()
        self.assertEqual(first['state'], 'SUBMITTED')
        self.assertEqual(self.post('/submissions', body).json()['state'], 'SUBMITTED')
        self.assert_one_collection()

    def evidence_bytes(self, row):
        """合成Git参照と全ジャーナルを直接比較する。本文や値を出力しない。"""
        return (self.git.submission_ref(row.id), tuple(
            (path.name, path.read_bytes()) for path in sorted(self.git.journal.iterdir())))

    def assert_unavailable_preserves_result(self, body, row, fault):
        before = self.evidence_bytes(row)
        with fault:
            self.assertEqual(self.post('/submissions', body).status_code, 503)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.TaskSubmission, row.id).state, 'SUBMITTED')
        self.assertEqual(self.evidence_bytes(row), before)
        self.assertEqual(self.post('/submissions', body).json()['state'], 'SUBMITTED')
        self.assert_one_collection()

    def test_git_lock_unavailable_preserves_result(self):
        from app.project_storage import StorageUnavailable
        body, row, _, _ = self.saved()
        self.assert_unavailable_preserves_result(body, row, patch.object(
            self.store, 'session_factory', side_effect=StorageUnavailable('synthetic lock timeout')))

    def test_journal_read_oserror_preserves_result(self):
        body, row, _, _ = self.saved()
        self.assert_unavailable_preserves_result(body, row, patch.object(
            self.git, 'journal_read', side_effect=OSError('synthetic read failure')))

    def test_storage_setup_conflict_is_not_evidence_corruption(self):
        from app.project_storage import StorageConflict
        body, row, _, _ = self.saved()
        self.assert_unavailable_preserves_result(body, row, patch.object(
            self.store, 'session_factory', side_effect=StorageConflict('synthetic setup conflict')))

    def test_unknown_verification_error_is_not_success(self):
        body, row, _, _ = self.saved()
        self.assert_unavailable_preserves_result(body, row, patch.object(
            self.store, 'recover', side_effect=RuntimeError('synthetic unknown failure')))

    def test_provider_authorization_denial_preserves_result(self):
        body, row, _, _ = self.saved()
        self.assert_unavailable_preserves_result(body, row, patch.object(
            self.provider, 'verify_authorize', side_effect=ProviderUnavailable('synthetic denial')))

    def test_factory_database_failure_is_not_evidence_corruption(self):
        body, row, request, receipt = self.saved()
        # 認可の製品処理は迂回せず、DBアクセス境界の一時障害を注入する。
        with patch.object(self.db, 'get', side_effect=RuntimeError('synthetic database unavailable')):
            with self.assertRaises(RuntimeError):
                self.provider.verify(request, receipt)
        self.assertEqual(self.post('/submissions', body).json()['state'], 'SUBMITTED')
        self.assert_one_collection()

    def test_saved_verification_and_recovery_have_no_git_write_path(self):
        body, row, _, _ = self.saved()
        before = self.evidence_bytes(row)
        # 読取り経路は書込みAPIを呼んだ時点で失敗する。Git証拠も直接バイト比較する。
        with patch.object(self.store, 'save', side_effect=AssertionError('unexpected save')), \
             patch.object(self.git, 'journal_create', side_effect=AssertionError('unexpected journal write')), \
             patch.object(self.git, 'create_submission_ref', side_effect=AssertionError('unexpected ref write')):
            self.assertEqual(self.post('/submissions', body).json()['state'], 'SUBMITTED')
            self.assertEqual(reconcile_submission(self.db, self.member, row.id, self.provider)['state'], 'SUBMITTED')
        self.assertEqual(self.evidence_bytes(row), before)
        self.assert_one_collection()

    def review_required_saved(self):
        body, row, request, receipt = self.saved()
        row.state = 'REVIEW_REQUIRED'
        self.db.commit()
        return body, row, request, receipt

    def test_reconcile_commit_before_execution_failure_preserves_evidence(self):
        _, row, _, _ = self.review_required_saved()
        before = self.evidence_bytes(row)
        with patch.object(self.db, 'commit', side_effect=RuntimeError('synthetic precommit failure')):
            with self.assertRaises(ProviderUnavailable):
                reconcile_submission(self.db, self.member, row.id, self.provider)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.TaskSubmission, row.id).state, 'REVIEW_REQUIRED')
        self.assertEqual(self.evidence_bytes(row), before)
        self.assertEqual(reconcile_submission(self.db, self.member, row.id, self.provider)['state'], 'SUBMITTED')
        self.assert_one_collection()

    def test_reconcile_ack_loss_database_reread_failure_keeps_committed_result(self):
        _, row, _, _ = self.review_required_saved()
        row_id = row.id
        before = self.evidence_bytes(row)
        original_commit, original_get = self.db.commit, self.db.get
        committed = []

        def commit():
            original_commit()
            committed.append(True)
            raise RuntimeError('synthetic lost acknowledgement')

        def get(entity, *args, **kwargs):
            if committed and entity is models.TaskSubmission:
                raise RuntimeError('synthetic fresh read failure')
            return original_get(entity, *args, **kwargs)

        with patch.object(self.db, 'commit', side_effect=commit), patch.object(self.db, 'get', side_effect=get):
            with self.assertRaises(ProviderUnavailable):
                reconcile_submission(self.db, self.member, row_id, self.provider)
        self.db.expire_all()
        row = self.db.get(models.TaskSubmission, row_id)
        self.assertEqual(row.state, 'SUBMITTED')
        self.assertEqual(self.evidence_bytes(row), before)
        self.assert_one_collection()

    def test_reconcile_ack_loss_evidence_reread_failure_keeps_committed_result(self):
        _, row, _, _ = self.review_required_saved()
        before = self.evidence_bytes(row)
        original_commit, original_recover = self.db.commit, self.store.recover
        committed = []

        def commit():
            original_commit()
            committed.append(True)
            raise RuntimeError('synthetic lost acknowledgement')

        def recover(request):
            if committed:
                raise OSError('synthetic evidence read failure')
            return original_recover(request)

        with patch.object(self.db, 'commit', side_effect=commit), patch.object(self.store, 'recover', side_effect=recover):
            with self.assertRaises(ProviderUnavailable):
                reconcile_submission(self.db, self.member, row.id, self.provider)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.TaskSubmission, row.id).state, 'SUBMITTED')
        self.assertEqual(self.evidence_bytes(row), before)
        self.assert_one_collection()

    def test_reconcile_ack_loss_revoked_authority_does_not_return_success(self):
        _, row, _, _ = self.review_required_saved()
        original_commit = self.db.commit

        def commit():
            original_commit()
            # 別トランザクションで権限が失効した順序を合成DB上に再現する。
            self.db.query(models.TeamMember).filter_by(team_id=self.team.id, user_id=self.member.id).delete()
            original_commit()
            raise RuntimeError('synthetic lost acknowledgement')

        with patch.object(self.db, 'commit', side_effect=commit):
            with self.assertRaises(ProviderUnavailable):
                reconcile_submission(self.db, self.member, row.id, self.provider)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.TaskSubmission, row.id).state, 'SUBMITTED')
        self.assert_one_collection()

    def test_reconcile_temporary_failure_preserves_submitted(self):
        _, row, _, _ = self.saved()
        before = self.evidence_bytes(row)
        with patch.object(self.git, 'journal_read', side_effect=OSError('synthetic evidence read failure')):
            with self.assertRaises(ProviderUnavailable):
                reconcile_submission(self.db, self.member, row.id, self.provider)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.TaskSubmission, row.id).state, 'SUBMITTED')
        self.assertEqual(self.evidence_bytes(row), before)
        self.assert_one_collection()

    def test_reconcile_unknown_failure_preserves_review_required(self):
        _, row, _, _ = self.review_required_saved()
        with patch.object(self.store, 'recover', side_effect=RuntimeError('synthetic internal error')):
            with self.assertRaises(ProviderUnavailable):
                reconcile_submission(self.db, self.member, row.id, self.provider)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.TaskSubmission, row.id).state, 'REVIEW_REQUIRED')
        self.assert_one_collection()

    def test_reconcile_preceding_confirmation_is_not_overwritten(self):
        _, row, _, receipt = self.review_required_saved()
        original = self.provider.verify

        def verify(request, snapshot):
            original(request, snapshot)
            other = self.db.get(models.TaskSubmission, row.id)
            other.state = 'SUBMITTED'
            other.submitted_at = datetime(2020, 1, 1)
            self.db.commit()

        with patch.object(self.provider, 'verify', side_effect=verify):
            with self.assertRaises(ProviderUnavailable):
                reconcile_submission(self.db, self.member, row.id, self.provider)
        self.db.expire_all()
        result = self.db.get(models.TaskSubmission, row.id)
        self.assertEqual((result.state, result.commit_id, result.submitted_at),
                         ('SUBMITTED', receipt.commit_id, datetime(2020, 1, 1)))
        self.assert_one_collection()

    def test_conflict_downgrade_cannot_overwrite_newer_confirmation(self):
        from app.submission_provider import EvidenceMismatch
        body, row, _, _ = self.saved()

        def verify(request, receipt):
            # 検査後に別処理が確定した状態を作り、古い検査結果のCAS拒否を確認する。
            row.submitted_at = datetime(2020, 1, 1)
            self.db.commit()
            raise EvidenceMismatch('synthetic stale conflict')

        with patch.object(self.provider, 'verify', side_effect=verify):
            self.assertEqual(self.post('/submissions', body).status_code, 503)
        self.db.expire_all()
        self.assertEqual(self.db.get(models.TaskSubmission, row.id).state, 'SUBMITTED')
        self.assert_one_collection()

    def test_dirty_session_cannot_authorize_new_collection(self):
        body = self.payload()
        self.sessions.evidence = replace(self.sessions.evidence, saved=False)
        result = self.post('/submissions', body).json()
        self.assertEqual(result['state'], 'REVIEW_REQUIRED')
        self.assertEqual(self.collection_states, [])
        self.assertEqual(self.transport.calls, 0)

    def test_expired_permit_cannot_admit_new_collection(self):
        body = self.payload()
        permit = self.db.query(models.SubmissionPermit).one()
        permit.created_at = datetime(1970, 1, 1)
        permit.expires_at = datetime(1970, 1, 1, 0, 1)
        self.db.commit()
        self.assertEqual(self.post('/submissions', body).status_code, 409)
        self.assertEqual(self.db.query(models.TaskSubmission).count(), 0)
        self.assertEqual(self.collection_states, [])
        self.assertEqual(self.transport.calls, 0)

    def test_dirty_change_before_database_publication_is_still_rejected(self):
        body = self.payload()
        original = self.provider.collect

        def collect(request):
            receipt = original(request)
            self.sessions.evidence = replace(self.sessions.evidence, saved=False)
            return receipt

        with patch.object(self.provider, 'collect', side_effect=collect):
            first = self.post('/submissions', body).json()
        self.assertEqual(first['state'], 'REVIEW_REQUIRED')
        # 履歴の明示復旧は現在のdirtyを過去へ遡及せず、同じGit証拠だけを確認する。
        self.assertEqual(reconcile_submission(self.db, self.member, first['id'], self.provider)['state'], 'SUBMITTED')
        self.assert_one_collection()

    def test_expiry_before_database_publication_is_still_rejected(self):
        body = self.payload()
        original = self.provider.collect

        def collect(request):
            receipt = original(request)
            self.now += 7200
            return receipt

        with patch.object(self.provider, 'collect', side_effect=collect):
            first = self.post('/submissions', body).json()
        self.assertEqual(first['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, first['id'], self.provider)['state'], 'SUBMITTED')
        self.assert_one_collection()

    def test_dirty_change_during_publication_evidence_check_is_rejected(self):
        body = self.payload()
        original = self.store.recover

        def recover(request):
            receipt = original(request)
            self.sessions.evidence = replace(self.sessions.evidence, saved=False)
            return receipt

        # Git保存そのものは置換せず、公開前の証拠読取り直後に状態を変える。
        with patch.object(self.store, 'recover', side_effect=recover):
            first = self.post('/submissions', body).json()
        self.assertEqual(first['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, first['id'], self.provider)['state'], 'SUBMITTED')
        self.assert_one_collection()
