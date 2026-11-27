"""固定の管理用補助プログラムで親の強制期限を検証。利用者コードは実行しない。"""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch,Mock
from app.managed_process import ManagedProcess,WorkerUnknown,MAX_MESSAGE
from app.worker_sandbox import restrict_filesystem

HELPER='''import sys,json,time,os
h=sys.stdin.buffer.read(4);job=json.loads(sys.stdin.buffer.read(int.from_bytes(h,"big")));sys.stdin.buffer.read()
if job["kind"]=="hang":time.sleep(20)
if job["kind"]=="partial":sys.stdout.buffer.write(b"\\x00\\x00\\x00\\x10x");sys.exit(0)
if job["kind"]=="exit":sys.exit(2)
if job["kind"]=="large":sys.stdout.buffer.write(b"x"*(15*1024*1024+10));sys.stdout.buffer.flush();sys.exit(0)
b=json.dumps({"complete":True,"credentials_inherited":any(k in os.environ for k in ["DATABASE_URL","HMAC_KEY","FIREBASE_CONFIG"])}).encode()
sys.stdout.buffer.write(len(b).to_bytes(4,"big")+b);sys.stdout.buffer.flush()
'''


class ManagedProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.program=Path(self.temp.name)/'fixed_manager_helper.py';self.program.write_text(HELPER,encoding='utf-8')
    def runner(self,timeout=2):return ManagedProcess(enabled=True,timeout=timeout,program=self.program)

    def test_complete_frame_and_no_credentials_inheritance(self):
        with patch.dict(os.environ,{'DATABASE_URL':'synthetic','HMAC_KEY':'synthetic','FIREBASE_CONFIG':'synthetic'}):
            self.assertEqual(self.runner().run({'kind':'normal'}),{'complete':True,'credentials_inherited':False})

    def test_disabled_never_starts_process(self):
        factory=Mock();runner=ManagedProcess(process_factory=factory)
        with self.assertRaises(WorkerUnknown):runner.run({})
        factory.assert_not_called()

    def test_timeout_kills_fixed_helper_and_quarantines_runner(self):
        runner=self.runner(0.2);start=time.monotonic()
        with self.assertRaises(WorkerUnknown):runner.run({'kind':'hang'})
        self.assertLess(time.monotonic()-start,3);self.assertTrue(runner.failed)
        with self.assertRaises(WorkerUnknown):runner.run({'kind':'normal'})

    def test_partial_frame_is_not_success(self):
        with self.assertRaises(WorkerUnknown):self.runner().run({'kind':'partial'})

    def test_crash_is_not_success(self):
        with self.assertRaises(WorkerUnknown):self.runner().run({'kind':'exit'})

    def test_oversized_output_is_rejected(self):
        with self.assertRaises(WorkerUnknown):self.runner().run({'kind':'large'})

    def test_unconfirmed_termination_is_not_retried(self):
        process=Mock(stdin=io.BytesIO(),stdout=io.BytesIO(),returncode=None)
        process.wait.side_effect=subprocess.TimeoutExpired('fixed',0.1)
        process.poll.return_value=None
        factory=Mock(return_value=process);runner=ManagedProcess(enabled=True,process_factory=factory)
        with self.assertRaises(WorkerUnknown):runner.run({})
        with self.assertRaises(WorkerUnknown):runner.run({})
        self.assertEqual(factory.call_count,1);self.assertTrue(runner.failed)

    def test_input_limit_before_launch(self):
        factory=Mock();runner=ManagedProcess(enabled=True,process_factory=factory)
        with self.assertRaises(WorkerUnknown):runner.run({'body':'x'*MAX_MESSAGE})
        factory.assert_not_called()

    def test_windows_cannot_claim_linux_sandbox_success(self):
        import sys
        if sys.platform=='linux':self.skipTest('Windows refusal contract')
        with self.assertRaises(RuntimeError):restrict_filesystem(-1,expected_parent=0)

    def test_management_import_does_not_load_database_or_firebase(self):
        import sys
        backend=str(Path(__file__).parents[1])
        code='import sys;sys.path.insert(0,'+repr(backend)+');import app.workspace_bootstrap;assert "app.db.database" not in sys.modules;assert "firebase_admin" not in sys.modules'
        result=subprocess.run([sys.executable,'-I','-B','-c',code],capture_output=True,
            env={k:os.environ[k] for k in ('SYSTEMROOT','WINDIR') if k in os.environ},timeout=5)
        self.assertEqual(result.returncode,0)
