"""権限条件はWindows合成属性で検証し、Linux固有試験は明示承認時だけ動かす。"""
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest

from app.stage10_preflight import regular_file_safe


class MetadataTests(unittest.TestCase):
    def info(self, mode=0o600, uid=1000, links=1, kind=stat.S_IFREG):
        return SimpleNamespace(st_mode=kind | mode, st_uid=uid, st_nlink=links)

    def test_private_saved_file_allowed(self):
        self.assertTrue(regular_file_safe(self.info(), 1000))

    def test_reported_666_rejected(self):
        self.assertFalse(regular_file_safe(self.info(mode=0o666), 1000))

    def test_644_allowed_without_relaxing_group_write(self):
        self.assertTrue(regular_file_safe(self.info(mode=0o644), 1000))
        self.assertFalse(regular_file_safe(self.info(mode=0o664), 1000))

    def test_other_owner_rejected(self):
        self.assertFalse(regular_file_safe(self.info(uid=1001), 1000))

    def test_hardlink_rejected(self):
        self.assertFalse(regular_file_safe(self.info(links=2), 1000))

    def test_symlink_rejected(self):
        self.assertFalse(regular_file_safe(self.info(kind=stat.S_IFLNK), 1000))

    def test_special_file_rejected(self):
        self.assertFalse(regular_file_safe(self.info(kind=stat.S_IFIFO), 1000))


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('STAGE10_SYNTHETIC_NATIVE') == 'approved',
                     'Requires approved isolated Linux synthetic environment')
class NativeBoundaryTests(unittest.TestCase):
    def test_real_peer_credentials(self):
        self.assertNotEqual(os.geteuid(), 0)
        a, b = socket.socketpair()
        try:
            pid, uid, gid = struct.unpack('3i', a.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            self.assertEqual((pid, uid, gid), (os.getpid(), os.geteuid(), os.getegid()))
        finally:
            a.close(); b.close()

    def test_real_landlock_initialization_and_scope(self):
        """新規テスト領域だけへ初期化する。実workspace・既存volumeは指定しない。"""
        from dataclasses import asdict
        from uuid import uuid4
        from app.managed_process import ManagedProcess, WorkerUnknown
        from app.submission_scope import SubmissionTarget
        root = Path(os.environ['STAGE10_SYNTHETIC_ROOT'])
        info = root.lstat()
        self.assertTrue(stat.S_ISDIR(info.st_mode) and not root.is_symlink())
        self.assertEqual((info.st_uid, stat.S_IMODE(info.st_mode)), (os.geteuid(), 0o700))
        # TemporaryDirectoryが作った領域だけを試験終了時に片付ける。
        with tempfile.TemporaryDirectory(dir=root, prefix='stage10b-native-') as directory:
            target = SubmissionTarget(1, 1, 1, 1, 'task')
            job = {'action': 'INITIALIZE', 'root': directory, 'owner_uid': os.geteuid(),
                   'owner_gid': os.getegid(), 'target': asdict(target), 'parent_pid': os.getpid(),
                   'base_commit': 'a' * 40, 'basis_id': str(uuid4()), 'proof': None,
                   'files': {'task/demo.txt': {'mode': '100644', 'data': 'c3ludGhldGlj'}}}
            runner = ManagedProcess(enabled=True, timeout=7)
            result = runner.run(job)
            self.assertEqual(result['proof']['basis_id'], job['basis_id'])
            verify = {**job, 'action': 'VERIFY', 'files': None, 'proof': result['proof']}
            self.assertTrue(runner.run(verify)['verified'])
            # 子の土地勘に任せず、固定root外への参照が成功しないことを確認する。
            invalid = {**verify, 'proof': {**result['proof'], 'workspace_id': '../outside'}}
            with self.assertRaises(WorkerUnknown):
                runner.run(invalid)
