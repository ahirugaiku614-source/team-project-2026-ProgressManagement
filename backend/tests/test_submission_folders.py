"""偽FDだけで検証する。Linux環境・実ファイル・本文へはアクセスしない。"""
import errno
import stat
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_projects  # modelsは試験用DATABASE_URLでimportする
from app.submission_folders import DirectoryOps, FolderUnavailable, ManagedTaskRoot, open_submission_folder
from app.submission_targets import SubmissionTarget, TargetForbidden, TargetInvalid


class FakeDirectories:
    def __init__(self):
        self.handles = {3: self.metadata(10)}
        self.next_fd = 20
        self.opened, self.closed = [], []
        self.fail = None
        self.mutation = False
        self.reads = {}

    @staticmethod
    def metadata(inode, *, mode=stat.S_IFDIR | 0o700, uid=1000, device=1):
        return SimpleNamespace(st_dev=device, st_ino=inode, st_uid=uid, st_mode=mode)

    def duplicate(self, fd):
        self.next_fd += 1
        self.handles[self.next_fd] = self.handles[fd]
        return self.next_fd

    def open_directory(self, parent, name):
        self.opened.append((parent, name))
        if self.fail == 'symlink':
            raise OSError(errno.ELOOP, 'blocked')
        self.next_fd += 1
        args = {'mode': stat.S_IFREG | 0o600} if self.fail == 'file' else {}
        if self.fail == 'owner':
            args['uid'] = 1001
        if self.fail == 'device':
            args['device'] = 2
        if self.fail == 'writable':
            args['mode'] = stat.S_IFDIR | 0o777
        self.handles[self.next_fd] = self.metadata(self.next_fd, **args)
        return self.next_fd

    def info(self, fd):
        self.reads[fd] = self.reads.get(fd, 0) + 1
        if self.mutation and self.reads[fd] > 1:
            return self.metadata(999)
        return self.handles[fd]

    def close(self, fd):
        self.closed.append(fd)


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.target = SubmissionTarget(1, 2, 3, 4, 'tasks/one')
        self.root = ManagedTaskRoot(1, 2, 4, 3, 1, 10, 1000)
        self.ops = FakeDirectories()

    def test_relative_fd_traversal_preserves_original_root_and_closes_owned_handles(self):
        with open_submission_folder(self.target, self.root, ops=self.ops) as fd:
            self.assertEqual(fd, 23)
            self.assertEqual([name for _, name in self.ops.opened], ['tasks', 'one'])
            self.assertEqual(self.ops.closed, [])
        self.assertEqual(self.ops.closed, [23, 22, 21])
        self.assertNotIn(3, self.ops.closed)

    def test_symlink_or_unsafe_directory_is_rejected_and_handles_closed(self):
        for fault in ('symlink', 'file', 'owner', 'device', 'writable'):
            with self.subTest(fault=fault):
                ops = FakeDirectories()
                ops.fail = fault
                with self.assertRaises(TargetInvalid):
                    with open_submission_folder(self.target, self.root, ops=ops):
                        self.fail('Unsafe directory was accepted')
                self.assertIn(21, ops.closed)

    def test_wrong_managed_root_scope_is_rejected_before_access(self):
        wrong = ManagedTaskRoot(1, 2, 99, 3, 1, 10, 1000)
        with self.assertRaises(TargetForbidden):
            with open_submission_folder(self.target, wrong, ops=self.ops):
                self.fail('Other student root accepted')
        self.assertEqual(self.ops.opened, [])
        self.assertEqual(self.ops.closed, [])

    def test_root_identity_replacement_is_rejected(self):
        self.ops.handles[3] = self.ops.metadata(99)
        with self.assertRaises(TargetInvalid):
            with open_submission_folder(self.target, self.root, ops=self.ops):
                self.fail('Changed root accepted')
        self.assertEqual(self.ops.opened, [])
        self.assertEqual(self.ops.closed, [21])

    def test_changed_fd_identity_before_yield_is_rejected(self):
        self.ops.mutation = True
        with self.assertRaises(TargetInvalid):
            with open_submission_folder(self.target, self.root, ops=self.ops):
                self.fail('Changed evidence accepted')
        self.assertEqual(self.ops.closed, [23, 22, 21])

    def test_cleanup_after_caller_failure(self):
        with self.assertRaises(RuntimeError):
            with open_submission_folder(self.target, self.root, ops=self.ops):
                raise RuntimeError('consumer stopped')
        self.assertEqual(self.ops.closed, [23, 22, 21])

    def test_default_adapter_fails_closed_on_windows(self):
        with patch('app.submission_folders.sys.platform', 'win32'):
            with self.assertRaises(FolderUnavailable):
                DirectoryOps()
