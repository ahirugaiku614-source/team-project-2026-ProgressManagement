"""DB→Gitの呼出しでもGit待ちを作らない契約。OS排他の実証とは区別する。"""
from contextlib import ExitStack, nullcontext
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.project_storage import ProjectStorage, StorageUnavailable
from app.review_git import ReviewGit


class ReviewLockPolicyTests(unittest.TestCase):
    def test_temporary_database_failure_does_not_revoke_work_basis(self):
        from app.task_review import TaskReviewService
        actor=SimpleNamespace(id=1)
        basis=SimpleNamespace(member_id=1,state='ACTIVE',team_id=1,project_id=1,task_id=1)
        db=Mock();db.get.return_value=basis
        service=TaskReviewService(db)
        with patch('app.task_review.lock_target',side_effect=StorageUnavailable('synthetic unavailable')):
            with self.assertRaises(StorageUnavailable):service._basis(None,actor,'synthetic-basis')
        self.assertEqual(basis.state,'ACTIVE');db.commit.assert_not_called()

    def patches(self, flock):
        stack=ExitStack()
        stack.enter_context(patch.dict('sys.modules',{'fcntl':SimpleNamespace(flock=flock,LOCK_EX=1,LOCK_NB=2)}))
        for name in ('O_NOFOLLOW','O_NONBLOCK'):
            stack.enter_context(patch('app.project_storage.os.'+name,0,create=True))
        stack.enter_context(patch('app.project_storage.os.open',return_value=21))
        stack.enter_context(patch('app.project_storage.os.fstat',return_value=SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_nlink=1)))
        close=stack.enter_context(patch('app.project_storage.os.close'))
        sleep=stack.enter_context(patch('app.project_storage.time.sleep'))
        stack.enter_context(patch('app.project_storage.time.monotonic',return_value=0))
        return stack,close,sleep

    def test_database_holder_does_not_wait_for_busy_git(self):
        flock=Mock(side_effect=BlockingIOError)
        stack,close,sleep=self.patches(flock)
        with stack:
            with self.assertRaises(StorageUnavailable):
                with object.__new__(ProjectStorage)._lock(9,wait=False):self.fail('busy lock entered')
        flock.assert_called_once_with(21,3);close.assert_called_once_with(21);sleep.assert_not_called()

    def test_initialization_retains_bounded_wait(self):
        flock=Mock(side_effect=[BlockingIOError,None])
        stack,close,sleep=self.patches(flock)
        with stack:
            with object.__new__(ProjectStorage)._lock(9):pass
        self.assertEqual(flock.call_count,2);sleep.assert_called_once_with(0.05)

    def test_review_and_integration_request_nonblocking_project_lock(self):
        storage=Mock()
        storage._project.return_value=nullcontext(9)
        # Mock自身の内部RLockとは別に、製品storageのメソッドを明示する。
        storage._lock=Mock(side_effect=StorageUnavailable('synthetic contention'))
        with self.assertRaises(StorageUnavailable):
            with ReviewGit(storage).session(1):self.fail('busy project entered')
        storage._lock.assert_called_once_with(9,wait=False)
        storage._state.assert_not_called()
