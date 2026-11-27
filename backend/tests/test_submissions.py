"""Isolated SQLite, fake identity verifier and fake collector; no real Git/FS."""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_projects as fixtures
import test_submission_targets as target_fixtures
from app import models
from app.db import database
from app.submission_provider import get_submission_provider, SubmissionSnapshot, DeniedSubmissionProvider, ProviderUnavailable
from sqlalchemy import inspect


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.transform = lambda result: result
        self.hook = lambda: None

    def ensure_ready(self): pass

    def verify(self, request, receipt):
        # Fake evidence only. Stage 3 tests independently verify real temporary Git.
        pass

    def collect(self, request):
        self.calls.append(request)
        self.hook()
        result = SubmissionSnapshot(request.submission_id, request.target,
            request.editing_session_id, f'refs/task-submissions/{request.submission_id}',
            'a' * 40, 1, 5, True)
        return self.transform(result)


class SubmissionAPITests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ProjectAPITests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectAPITests.tearDownClass.__func__)
    tearDown = fixtures.ProjectAPITests.tearDown
    headers = staticmethod(fixtures.ProjectAPITests.headers)
    decode_token = staticmethod(fixtures.ProjectAPITests.decode_token)
    register = target_fixtures.TargetTests.register

    def setUp(self):
        target_fixtures.TargetTests.setUp(self)
        self.register()
        self.db.commit()
        self.base = f'/api/teams/{self.team.id}/projects/{self.project.id}/tasks/{self.task.id}'
        self.session = str(uuid4())
        self.provider = FakeProvider()
        self.main.app.dependency_overrides[get_submission_provider] = lambda: self.provider

    def post(self, suffix, payload, uid='member'):
        return self.client.post(self.base + suffix, json=payload, headers=self.headers(uid))

    def permit(self):
        response = self.post('/submission-permits', {'editing_session_id': self.session})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return response.json()['permit']

    def payload(self):
        return dict(editing_session_id=self.session, request_id=str(uuid4()),
                    permit=self.permit(), has_unsaved_changes=False)

    def test_success_and_duplicate_call_collector_once(self):
        body = self.payload()
        response = self.post('/submissions', body)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['state'], 'SUBMITTED')
        self.assertNotIn(body['permit'], response.text)
        self.assertNotIn('folder_path', response.json())
        repeat = self.post('/submissions', body)
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json(), response.json())
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.task.status, models.TaskStatus.TODO)

    def test_default_provider_rejects_without_consumption(self):
        body = self.payload()
        self.main.app.dependency_overrides.pop(get_submission_provider)
        response = self.post('/submissions', body)
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(self.db.query(models.SubmissionPermit).one().consumed_at)
        self.assertEqual(self.db.query(models.TaskSubmission).count(), 0)
        with self.assertRaises(ProviderUnavailable):
            DeniedSubmissionProvider().collect(None)

    def test_token_not_stored_as_plaintext(self):
        token = self.permit()
        row = self.db.query(models.SubmissionPermit).one()
        self.assertEqual(len(row.token_digest), 64)
        self.assertNotIn(token, str(row.__dict__))

    def test_missing_authentication(self):
        response = self.client.post(self.base + '/submission-permits', json={'editing_session_id': self.session})
        self.assertIn(response.status_code, (401, 403))

    def test_other_students_and_owner_cannot_issue(self):
        for uid in ('owner', 'outsider', 'invalid'):
            self.assertIn(self.post('/submission-permits', {'editing_session_id': self.session}, uid).status_code, (401, 403))
        self.assertEqual(self.db.query(models.SubmissionPermit).count(), 0)

    def test_invalid_target_binding(self):
        self.base = self.base.replace(f'/projects/{self.project.id}', f'/projects/{self.foreign.id}')
        self.assertEqual(self.post('/submission-permits', {'editing_session_id': self.session}).status_code, 404)

    def test_revocation_after_issue(self):
        body = self.payload()
        self.db.query(models.TeamMember).filter_by(team_id=self.team.id, user_id=self.member.id).delete()
        self.db.commit()
        self.assertEqual(self.post('/submissions', body).status_code, 403)
        self.assertEqual(self.provider.calls, [])

    def test_reassignment_after_issue(self):
        body = self.payload()
        self.task.assignee_id = self.owner.id
        self.db.commit()
        self.assertEqual(self.post('/submissions', body).status_code, 403)
        self.assertEqual(self.provider.calls, [])

    def test_expired_permit(self):
        body = self.payload()
        row = self.db.query(models.SubmissionPermit).one()
        row.created_at = datetime.utcnow() - timedelta(minutes=10)
        row.expires_at = datetime.utcnow() - timedelta(minutes=5)
        self.db.commit()
        self.assertEqual(self.post('/submissions', body).status_code, 409)
        self.assertEqual(self.provider.calls, [])

    def test_wrong_session_and_token(self):
        body = self.payload()
        for change in ({'editing_session_id': str(uuid4())}, {'permit': 'x' * 43}):
            self.assertEqual(self.post('/submissions', {**body, **change}).status_code, 403)
        self.assertEqual(self.provider.calls, [])

    def test_consumed_permit_cannot_create_second_submission(self):
        body = self.payload()
        self.post('/submissions', body)
        self.assertEqual(self.post('/submissions', {**body, 'request_id': str(uuid4())}).status_code, 409)
        self.assertEqual(len(self.provider.calls), 1)

    def test_same_request_with_different_parameters(self):
        body = self.payload()
        self.post('/submissions', body)
        for change in ({'permit': self.permit()}, {'editing_session_id': str(uuid4())}):
            self.assertEqual(self.post('/submissions', {**body, **change}).status_code, 409)
        self.assertEqual(len(self.provider.calls), 1)

    def test_dirty_rejected_without_consuming(self):
        body = self.payload()
        self.assertEqual(self.post('/submissions', {**body, 'has_unsaved_changes': True}).status_code, 409)
        self.assertIsNone(self.db.query(models.SubmissionPermit).one().consumed_at)
        self.assertEqual(self.provider.calls, [])

    def test_client_cannot_select_path_url_or_student(self):
        body = self.payload()
        for field in ('absolute_path', 'url', 'student_id', 'files'):
            response = self.post('/submissions', {**body, field: 'private-value'})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('private-value', response.text)
            self.assertNotIn(body['permit'], response.text)
        self.assertEqual(self.provider.calls, [])

    def test_dirty_requires_strict_boolean(self):
        body = self.payload()
        for value in ('false', 0, None):
            self.assertEqual(self.post('/submissions', {**body, 'has_unsaved_changes': value}).status_code, 422)

    def test_unknown_receipt_no_retry(self):
        body = self.payload()
        self.provider.transform = lambda receipt: None
        first = self.post('/submissions', body)
        self.assertEqual(first.json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(len(self.provider.calls), 1)

    def test_collector_exception_no_retry_or_exception_leak(self):
        body = self.payload()
        def lost(): raise RuntimeError('private-error-details')
        self.provider.hook = lost
        response = self.post('/submissions', body)
        self.assertEqual(response.json()['state'], 'REVIEW_REQUIRED')
        self.assertNotIn('private-error-details', response.text)
        self.post('/submissions', body)
        self.assertEqual(len(self.provider.calls), 1)

    def test_receipt_identity_limits_and_dirty_evidence(self):
        mutations = [dict(submission_id=str(uuid4())), dict(editing_session_id=str(uuid4())),
            dict(git_ref='refs/heads/main'), dict(commit_id='invalid'), dict(file_count=True),
            dict(file_count=1001), dict(total_bytes=-1), dict(total_bytes=10485761),
            dict(saved_files_confirmed=False)]
        for values in mutations:
            self.provider.transform = lambda result, v=values: replace(result, **v)
            response = self.post('/submissions', self.payload())
            self.assertEqual(response.json()['state'], 'REVIEW_REQUIRED')

    def test_history_survives_task_deletion(self):
        self.post('/submissions', self.payload())
        self.db.delete(self.task)
        self.db.commit()
        self.assertEqual(self.db.query(models.TaskSubmission).count(), 1)
        self.assertEqual(self.db.query(models.SubmissionPermit).count(), 1)

    def test_processing_admission_is_visible_before_collect(self):
        def check():
            self.assertEqual(self.db.query(models.TaskSubmission).one().state, 'PROCESSING')
            self.assertIsNotNone(self.db.query(models.SubmissionPermit).one().consumed_at)
        self.provider.hook = check
        self.assertEqual(self.post('/submissions', self.payload()).json()['state'], 'SUBMITTED')

    def test_revocation_during_collection_blocks_success(self):
        def revoke():
            self.task.assignee_id = self.owner.id
            self.db.commit()
        self.provider.hook = revoke
        self.assertEqual(self.post('/submissions', self.payload()).json()['state'], 'REVIEW_REQUIRED')

    def test_pending_request_never_restarts_collection(self):
        body = self.payload()
        self.post('/submissions', body)
        row = self.db.query(models.TaskSubmission).one()
        row.state = 'PROCESSING'
        self.db.commit()
        self.assertEqual(self.post('/submissions', body).json()['state'], 'PROCESSING')
        self.assertEqual(len(self.provider.calls), 1)

    def test_database_failure_after_collection_preserves_admission_and_no_retry(self):
        body = self.payload()
        original = self.db.commit
        calls = []
        def commit():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError('database-finalization-failed')
            return original()
        def after_admission():
            patcher = patch.object(self.db, 'commit', side_effect=commit)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.provider.hook = after_admission
        first = self.post('/submissions', body)
        self.assertEqual(first.json()['state'], 'REVIEW_REQUIRED')
        self.assertIsNotNone(self.db.query(models.SubmissionPermit).one().consumed_at)
        self.post('/submissions', body)
        self.assertEqual(len(self.provider.calls), 1)

    def test_database_commit_ack_loss_does_not_overwrite_committed_result(self):
        body = self.payload()
        original = self.db.commit
        def committed_then_lost():
            original()
            raise RuntimeError('acknowledgment-lost')
        def after_admission():
            patcher = patch.object(self.db, 'commit', side_effect=committed_then_lost)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.provider.hook = after_admission
        response = self.post('/submissions', body)
        self.assertEqual(response.json()['state'], 'SUBMITTED')
        self.assertEqual(self.post('/submissions', body).json(), response.json())
        self.assertEqual(len(self.provider.calls), 1)

    def test_foreign_target_receipt_is_rejected(self):
        body = self.payload()
        def wrong_target(receipt):
            return replace(receipt, target=replace(receipt.target, task_id=receipt.target.task_id + 100))
        self.provider.transform = wrong_target
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')

    def test_admission_failure_does_not_call_collector(self):
        body = self.payload()
        with patch.object(self.db, 'commit', side_effect=RuntimeError('admission-failed')):
            response = self.post('/submissions', body)
        self.assertEqual(response.status_code, 500)
        self.db.rollback()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.db.query(models.TaskSubmission).count(), 0)
        self.assertIsNone(self.db.query(models.SubmissionPermit).one().consumed_at)


class SubmissionMigrationTests(unittest.TestCase):
    def test_explicit_migration_only_and_history_has_no_task_fk(self):
        engine = fixtures.test_engine()
        self.addCleanup(fixtures.close_test_engine, engine)
        database.create_startup_tables(engine)
        self.assertFalse(inspect(engine).has_table('submission_permits'))
        self.assertFalse(inspect(engine).has_table('task_submissions'))
        script = (Path(__file__).parents[1] / 'migrations/006_task_submissions.sql').read_text(encoding='utf-8')
        connection = engine.raw_connection()
        try:
            connection.driver_connection.executescript(script)
            for name in ('submission_permits', 'task_submissions'):
                self.assertTrue(inspect(engine).has_table(name))
                self.assertNotIn('tasks', [f['referred_table'] for f in inspect(engine).get_foreign_keys(name)])
            with self.assertRaises(sqlite3.OperationalError):
                connection.driver_connection.executescript(script)
            connection.rollback()
        finally:
            connection.close()
