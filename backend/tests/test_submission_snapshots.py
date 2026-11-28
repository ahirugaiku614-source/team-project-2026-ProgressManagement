from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import json
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_projects  # isolated database imports; no real credentials
import test_submissions as api_fixtures
from app import models
from app.submission_targets import SubmissionTarget
from app.submission_folders import ManagedTaskRoot
from app.submission_provider import SubmissionCollection, get_submission_provider, DeniedSubmissionProvider
from app.submission_collection import collect_task
from app.submission_git import GitSnapshotStore, ManagedSnapshotProvider, decode_record
from app.submissions import reconcile_submission
from app.project_storage import StorageConflict
from app.review_git import ReviewInvalid, ReviewLimit
from app.submission_targets import TargetForbidden
from submission_snapshot_fakes import FakeTaskOps, PortableGitFixture


def request():
    return SubmissionCollection(str(uuid4()), str(uuid4()), str(uuid4()),
        SubmissionTarget(1, 2, 3, 4, 'assignments/one'))


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.request = request(); self.ops = FakeTaskOps()
        self.root = ManagedTaskRoot(1, 2, 4, 3, 1, 1, 1000)

    def collect(self): return collect_task(self.request, self.root, self.ops)

    def test_only_registered_task(self):
        self.assertEqual(set(self.collect()), {'main.py'})
        self.assertEqual(self.ops.reads, ['assignments/one/main.py'] * 2)

    def test_other_student_root_rejected_before_read(self):
        self.root = replace(self.root, student_id=99)
        with self.assertRaises(TargetForbidden): self.collect()
        self.assertEqual(self.ops.reads, [])

    def test_other_project_root_rejected(self):
        self.root = replace(self.root, project_id=99)
        with self.assertRaises(TargetForbidden): self.collect()
        self.assertEqual(self.ops.reads, [])

    def test_symlink_hardlink_and_special_rejected(self):
        for values in ({'mode': stat.S_IFLNK | 0o700}, {'links': 2},
                       {'mode': stat.S_IFIFO | 0o600}, {'mode': stat.S_IFSOCK | 0o600}):
            with self.subTest(values=values):
                self.ops = FakeTaskOps(); self.ops.nodes['assignments/one/main.py'].update(values)
                with self.assertRaises(ReviewInvalid): self.collect()
                self.assertEqual(self.ops.reads, [])

    def test_wrong_owner_or_device_rejected(self):
        for values in ({'uid': 9}, {'device': 9}, {'mode': stat.S_IFREG | 0o666}):
            self.ops = FakeTaskOps(); self.ops.nodes['assignments/one/main.py'].update(values)
            with self.assertRaises(ReviewInvalid): self.collect()

    def test_fixed_exclusions_not_gitignore(self):
        for name in ('.env', '.env.local', 'secret.pem', 'firebase-credentials.json'):
            self.ops.nodes['assignments/one/' + name] = {'data': b'excluded'}
        self.ops.nodes['assignments/one/node_modules'] = {}
        self.ops.nodes['assignments/one/node_modules/private.js'] = {'data': b'excluded'}
        self.ops.nodes['assignments/one/.gitignore'] = {'data': b'*.py\n'}
        self.assertEqual(set(self.collect()), {'main.py'})
        self.assertEqual(self.ops.reads, ['assignments/one/main.py'] * 2)

    def test_excluded_symlink_still_rejected(self):
        self.ops.nodes['assignments/one/.env'] = {'mode': stat.S_IFLNK | 0o700}
        with self.assertRaises(ReviewInvalid): self.collect()

    def test_contents_changed_with_same_metadata(self):
        self.ops.change_contents = True
        with self.assertRaises(StorageConflict): self.collect()

    def test_identity_changed_during_read(self):
        self.ops.changed_identity = True
        with self.assertRaises(StorageConflict): self.collect()

    def test_file_size_limit_before_read(self):
        self.ops.nodes['assignments/one/main.py']['size'] = 1048577
        with self.assertRaises(ReviewLimit): self.collect()
        self.assertEqual(self.ops.reads, [])

    def test_file_count_limit(self):
        self.ops.nodes['assignments/one/second.py'] = {'data': b'x'}
        with patch('app.submission_collection.MAX_FILES', 1):
            with self.assertRaises(ReviewLimit): self.collect()

    def test_total_size_limit(self):
        with patch('app.submission_collection.MAX_TOTAL_BYTES', 1):
            with self.assertRaises(ReviewLimit): self.collect()

    def test_entry_limit(self):
        with patch('app.submission_collection.MAX_ENTRIES', 0):
            with self.assertRaises(ReviewLimit): self.collect()

    def test_depth_limit(self):
        self.ops.nodes['assignments/one/subdir'] = {}
        with patch('app.submission_collection.MAX_DEPTH', 0):
            with self.assertRaises(ReviewLimit): self.collect()

    def test_empty_task_rejected(self):
        del self.ops.nodes['assignments/one/main.py']
        with self.assertRaises(ReviewInvalid): self.collect()

    def test_owner_changes_during_read(self):
        original = self.ops.read_file
        def read(fd, name, expected):
            body = original(fd, name, expected)
            self.ops.nodes['assignments/one/main.py']['uid'] = 9
            return body
        self.ops.read_file = read
        with self.assertRaises(StorageConflict): self.collect()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.git = PortableGitFixture(self.temp.name)
        self.store = GitSnapshotStore(self.git.session)
        self.request = request(); self.calls = 0

    def collect(self):
        self.calls += 1
        return {'main.py': ('100644', b'fixed fixture\n')}

    def save(self): return self.store.save(self.request, self.collect, lambda: None)

    def test_real_git_tree_and_no_main_or_review_ref(self):
        receipt = self.save()
        self.assertEqual(set(self.git.entries(receipt.commit_id)), {'main.py'})
        self.assertEqual(self.git.run('rev-list', '--parents', '-n', '1', receipt.commit_id).split(), [receipt.commit_id.encode()])
        self.assertEqual(self.git.run('for-each-ref', '--format=%(refname)').decode().splitlines(), [receipt.git_ref])
        self.assertEqual(self.store.recover(self.request), receipt)

    def test_second_call_never_recollects(self):
        self.save()
        with self.assertRaises(Exception): self.save()
        self.assertEqual(self.calls, 1)

    def test_parallel_same_id_collects_once(self):
        results = []
        def worker():
            try: self.save(); results.append('saved')
            except Exception: results.append('rejected')
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(results, ['saved', 'rejected'])
        self.assertEqual(self.calls, 1)

    def test_parallel_distinct_ids_preserved(self):
        requests = [self.request, replace(self.request, submission_id=str(uuid4()), request_id=str(uuid4()))]
        results = []
        def worker(r): results.append(self.store.save(r, lambda: {'main.py': ('100644', b'x')}, lambda: None))
        threads = [threading.Thread(target=worker, args=(r,)) for r in requests]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results[0].git_ref, results[1].git_ref)

    def test_git_failure_retains_intent_no_recollect(self):
        self.git.failure = 'git'
        with self.assertRaises(Exception): self.save()
        self.git.failure = None
        with self.assertRaises(Exception): self.save()
        self.assertEqual(self.calls, 1)
        with self.assertRaises(Exception): self.store.recover(self.request)

    def test_ref_failure_prepared_journal_not_success(self):
        self.git.failure = 'ref'
        with self.assertRaises(Exception): self.save()
        self.assertIsNotNone(self.git.journal_read(self.request.submission_id, 'prepared'))
        self.assertIsNone(self.git.submission_ref(self.request.submission_id))
        with self.assertRaises(Exception): self.store.recover(self.request)

    def test_complete_failure_preserves_commit_but_blocks_recovery(self):
        self.git.failure = 'complete'
        with self.assertRaises(Exception): self.save()
        self.assertIsNotNone(self.git.submission_ref(self.request.submission_id))
        with self.assertRaises(Exception): self.store.recover(self.request)
        self.assertEqual(self.calls, 1)

    def test_corrupt_journal_rejected(self):
        self.save()
        (self.git.journal / (self.request.submission_id + '.complete')).write_bytes(b'{')
        with self.assertRaises(Exception): self.store.recover(self.request)

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(Exception): decode_record(b'{"version":1,"version":1}')

    def test_foreign_task_or_session_journal_rejected(self):
        self.save()
        for r in (replace(self.request, target=replace(self.request.target, task_id=99)),
                  replace(self.request, editing_session_id=str(uuid4()))):
            with self.assertRaises(Exception): self.store.recover(r)

    def test_missing_ref_rejected_without_recreation(self):
        receipt = self.save()
        self.git.run('update-ref', '-d', receipt.git_ref)
        with self.assertRaises(Exception): self.store.recover(self.request)
        self.assertIsNone(self.git.submission_ref(self.request.submission_id))

    def test_ref_cannot_be_overwritten(self):
        receipt = self.save()
        with self.assertRaises(Exception): self.git.create_submission_ref(self.request.submission_id, receipt.commit_id)
        self.assertEqual(self.git.submission_ref(self.request.submission_id), receipt.commit_id)

    def test_modified_tree_receipt_rejected(self):
        self.save()
        for phase in ('prepared', 'complete'):
            path = self.git.journal / (self.request.submission_id + '.' + phase)
            record = json.loads(path.read_bytes()); record['receipt']['total_bytes'] += 1
            path.write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaises(Exception): self.store.recover(self.request)

    def test_authorization_revoked_before_save(self):
        def denied(): raise PermissionError()
        with self.assertRaises(Exception): self.store.save(self.request, self.collect, denied)
        self.assertEqual(self.calls, 0)

    def test_authorization_revoked_after_collect_before_git(self):
        def check():
            if self.calls: raise PermissionError()
        with self.assertRaises(Exception): self.store.save(self.request, self.collect, check)
        self.assertIsNone(self.git.submission_ref(self.request.submission_id))

    def test_reference_mismatch_rejected_and_not_repaired(self):
        receipt = self.save()
        tree = self.git.tree({'different.py': ('100644', b'other')})
        other = self.git.run('commit-tree', tree, data=b'Other fixture\n').decode().strip()
        self.git.run('update-ref', receipt.git_ref, other)
        with self.assertRaises(StorageConflict): self.store.recover(self.request)
        self.assertEqual(self.git.submission_ref(self.request.submission_id), other)

    def test_boolean_journal_identity_is_not_an_integer(self):
        self.save()
        path = self.git.journal / (self.request.submission_id + '.intent')
        record = json.loads(path.read_bytes()); record['version'] = True
        path.write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaises(StorageConflict): self.store.recover(self.request)

    def test_authorization_revoked_after_git_preserves_snapshot_without_success(self):
        checks = []
        def check():
            checks.append(1)
            if len(checks) == 4: raise PermissionError()
        with self.assertRaises(PermissionError): self.store.save(self.request, self.collect, check)
        self.assertIsNotNone(self.git.submission_ref(self.request.submission_id))
        self.assertIsNone(self.git.journal_read(self.request.submission_id, 'complete'))
        with self.assertRaises(StorageConflict): self.store.recover(self.request)
        self.assertEqual(self.calls, 1)


class SnapshotAPITests(unittest.TestCase):
    setUpClass = api_fixtures.SubmissionAPITests.__dict__['setUpClass']
    tearDownClass = api_fixtures.SubmissionAPITests.__dict__['tearDownClass']
    tearDown = api_fixtures.SubmissionAPITests.tearDown
    headers = staticmethod(api_fixtures.SubmissionAPITests.headers)
    decode_token = staticmethod(api_fixtures.SubmissionAPITests.decode_token)
    register = api_fixtures.SubmissionAPITests.register
    post = api_fixtures.SubmissionAPITests.post
    permit = api_fixtures.SubmissionAPITests.permit
    payload = api_fixtures.SubmissionAPITests.payload

    def setUp(self):
        api_fixtures.SubmissionAPITests.setUp(self)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.git = PortableGitFixture(self.temp.name); self.ops = FakeTaskOps()
        # API fixture registers assignments/task-1; mirror that server binding.
        self.ops.nodes = {path.replace('assignments/one', 'assignments/task-1'): value
                          for path, value in self.ops.nodes.items()}
        self.source_calls = []
        @contextmanager
        def source(request):
            self.source_calls.append(request)
            t = request.target
            yield ManagedTaskRoot(t.team_id, t.project_id, t.student_id, 3, 1, 1, 1000), self.ops
        def authorized(request):
            from app.submission_targets import resolve_submission_target
            try:
                self.db.expire_all()
                target = resolve_submission_target(self.db, self.member, team_id=request.target.team_id,
                    project_id=request.target.project_id, task_id=request.target.task_id)
                row = self.db.get(models.TaskSubmission, request.submission_id)
                return target == request.target and row is not None
            except Exception: return False
        self.provider = ManagedSnapshotProvider(source, GitSnapshotStore(self.git.session), authorized, lambda r: True)
        self.main.app.dependency_overrides[get_submission_provider] = lambda: self.provider

    def test_api_snapshot_and_duplicate_no_recapture(self):
        body = self.payload(); first = self.post('/submissions', body)
        self.assertEqual(first.json()['state'], 'SUBMITTED')
        self.assertEqual(self.post('/submissions', body).json(), first.json())
        self.assertEqual(len(self.source_calls), 1)
        self.assertNotIn('fixed fixture', first.text)

    def test_db_failure_reconcile_existing_commit_without_source(self):
        body = self.payload(); original = self.db.commit; calls = []
        def commit():
            calls.append(1)
            if len(calls) == 2: raise RuntimeError('synthetic database failure')
            return original()
        with patch.object(self.db, 'commit', side_effect=commit):
            first = self.post('/submissions', body).json()
        self.assertEqual(first['state'], 'REVIEW_REQUIRED')
        row = self.db.query(models.TaskSubmission).one()
        oid = self.git.submission_ref(row.id)
        self.assertIsNotNone(oid)
        result = reconcile_submission(self.db, self.member, row.id, self.provider)
        self.assertEqual(result['state'], 'SUBMITTED'); self.assertEqual(result['commit_id'], oid)
        self.assertEqual(len(self.source_calls), 1)

    def test_git_failure_cannot_be_confirmed_or_retried(self):
        body = self.payload(); self.git.failure = 'ref'
        first = self.post('/submissions', body).json()
        self.assertEqual(first['state'], 'REVIEW_REQUIRED')
        self.git.failure = None
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, first['id'], self.provider)['state'], 'REVIEW_REQUIRED')
        self.assertEqual(len(self.source_calls), 1)

    def test_database_commit_mismatch_never_confirmed(self):
        body = self.payload(); first = self.post('/submissions', body).json()
        row = self.db.query(models.TaskSubmission).one(); row.commit_id = 'b' * 40; self.db.commit()
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(reconcile_submission(self.db, self.member, first['id'], self.provider)['state'], 'REVIEW_REQUIRED')

    def test_missing_git_ref_downgrades_existing_success(self):
        body = self.payload(); first = self.post('/submissions', body).json()
        self.git.run('update-ref', '-d', first['git_ref'])
        self.assertEqual(self.post('/submissions', body).json()['state'], 'REVIEW_REQUIRED')
        self.assertEqual(len(self.source_calls), 1)

    def test_saved_session_unavailable_refuses_collection(self):
        self.provider.saved_session = lambda r: False
        response = self.post('/submissions', self.payload()).json()
        self.assertEqual(response['state'], 'REVIEW_REQUIRED')
        self.assertEqual(self.source_calls, [])

    def test_recovery_requires_matching_consumption_evidence(self):
        body = self.payload(); first = self.post('/submissions', body).json()
        permit = self.db.query(models.SubmissionPermit).one()
        permit.consumed_at = None; self.db.commit()
        self.assertEqual(reconcile_submission(self.db, self.member, first['id'], self.provider)['state'], 'REVIEW_REQUIRED')
        self.assertEqual(len(self.source_calls), 1)
