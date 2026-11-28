"""一時SQLiteと偽socketだけを使う。Linux実UDSの成功とは扱わない。"""
import sqlite3
import struct
import tempfile
import threading
from pathlib import Path
from uuid import uuid4
import unittest
from unittest.mock import patch
from app.submission_ledger import SQLiteReplayLedger, ClosingConnection
from app.submission_server import UDSServer
from app.submission_server import socket_parent
from app.submission_provider import ProviderUnavailable


def fake_guard(path):
    # WindowsではPOSIX権限を保証できないため、存在とinodeだけを模擬する。
    s=path.stat(); return s.st_dev,s.st_ino


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'ledger.sqlite'
        self.ledger=SQLiteReplayLedger.provision(self.path,guard=fake_guard)

    def test_reopen_rejects_used_id(self):
        key=str(uuid4());self.ledger.claim(key)
        reopened=SQLiteReplayLedger(self.path,guard=fake_guard)
        with self.assertRaises(ProviderUnavailable):reopened.claim(key)

    def test_parallel_claim_once(self):
        key=str(uuid4()); results=[]
        def run():
            try:SQLiteReplayLedger(self.path,guard=fake_guard).claim(key);results.append(True)
            except ProviderUnavailable:results.append(False)
        ts=[threading.Thread(target=run) for _ in range(2)]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertCountEqual(results,[True,False])

    def test_unknown_remains_used(self):
        key=str(uuid4());self.ledger.claim(key);self.ledger.finish(key,'UNKNOWN')
        with self.assertRaises(ProviderUnavailable):self.ledger.claim(key)

    def test_completed_remains_used(self):
        key=str(uuid4());self.ledger.claim(key);self.ledger.finish(key,'COMPLETE')
        with self.assertRaises(ProviderUnavailable):self.ledger.claim(key)

    def test_corruption_rejected(self):
        self.path.write_bytes(b'broken')
        with self.assertRaises(ProviderUnavailable):SQLiteReplayLedger(self.path,guard=fake_guard)

    def test_missing_is_not_recreated(self):
        self.path.unlink()
        with self.assertRaises(ProviderUnavailable):SQLiteReplayLedger(self.path,guard=fake_guard)
        self.assertFalse(self.path.exists())

    def test_write_failure_sticky(self):
        with patch.object(self.ledger,'_connect',side_effect=sqlite3.OperationalError()):
            with self.assertRaises(ProviderUnavailable):self.ledger.claim(str(uuid4()))
        self.assertTrue(self.ledger.failed)

    def test_provision_never_overwrites(self):
        before=self.path.read_bytes()
        with self.assertRaises(FileExistsError):SQLiteReplayLedger.provision(self.path,guard=fake_guard)
        self.assertEqual(self.path.read_bytes(),before)

    def test_locked_ledger_rejects_and_stays_failed(self):
        db=sqlite3.connect(self.path);self.addCleanup(db.close)
        db.execute('BEGIN IMMEDIATE')
        with self.assertRaises(ProviderUnavailable):self.ledger.claim(str(uuid4()))
        db.rollback();self.assertTrue(self.ledger.failed)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.calls=[];case=self
        class Handler:
            def handle(self,frame):case.calls.append(frame);return b'\0\0\0\1x'
        self.server=UDSServer('/run/synthetic/c.sock',Handler(),2001,2002)
        self.wire=b'\0\0\0\1x';self.peer=(2001,2002);self.sent=[]
        class Connection:
            def getsockopt(self,*a):return struct.pack('3i',1,*case.peer)
            def settimeout(self,t):pass
            def recv(self,n):out=case.wire[:n];case.wire=case.wire[n:];return out
            def sendall(self,data):case.sent.append(data)
        self.connection=Connection()
        p=patch('app.submission_server.socket.SO_PEERCRED',17,create=True);p.start();self.addCleanup(p.stop)

    def test_authenticated_frame(self):
        self.server.process_connection(self.connection);self.assertEqual(len(self.calls),1)

    def test_wrong_uid_and_gid_before_handler(self):
        for peer in ((9,2002),(2001,9)):
            self.peer=peer
            with self.assertRaises(ProviderUnavailable):self.server.process_connection(self.connection)
        self.assertEqual(self.calls,[])

    def test_partial_disconnect(self):
        self.wire=b'\0\0'
        with self.assertRaises(ProviderUnavailable):self.server.process_connection(self.connection)
        self.assertEqual(self.calls,[])

    def test_oversized_frame(self):
        self.wire=(16385).to_bytes(4,'big')
        with self.assertRaises(ProviderUnavailable):self.server.process_connection(self.connection)
        self.assertEqual(self.calls,[])

    def test_extra_frame_rejected(self):
        self.wire*=2
        with self.assertRaises(ProviderUnavailable):self.server.process_connection(self.connection)
        self.assertEqual(self.calls,[])

    def test_disabled_start_does_not_bind(self):
        with patch('app.submission_server.socket.socket') as factory:
            with self.assertRaises(ProviderUnavailable):self.server.start()
            factory.assert_not_called()

    def test_timeout_before_collection(self):
        ticks=iter([0,6]);self.server.monotonic=lambda:next(ticks)
        with self.assertRaises(ProviderUnavailable):self.server.process_connection(self.connection)
        self.assertEqual(self.calls,[])

    def test_symlink_parent_rejected(self):
        from types import SimpleNamespace
        import stat
        with patch('app.submission_server.Path.lstat',return_value=SimpleNamespace(st_mode=stat.S_IFLNK|0o777,st_uid=0)):
            with self.assertRaises(ProviderUnavailable):socket_parent('/run/synthetic/c.sock')

    def test_existing_endpoint_not_deleted(self):
        self.server.enabled=True
        with patch('app.submission_server.sys.platform','linux'),patch('app.submission_server.socket_parent'),patch('app.submission_server.os.path.lexists',return_value=True),patch('app.submission_server.os.unlink') as unlink:
            with self.assertRaises(ProviderUnavailable):self.server.start()
            unlink.assert_not_called()

    def test_socket_parent_permissions_rejected(self):
        from types import SimpleNamespace
        import stat
        with patch('app.submission_server.Path.lstat',return_value=SimpleNamespace(st_mode=stat.S_IFDIR|0o777,st_uid=0)):
            with self.assertRaises(ProviderUnavailable):socket_parent('/run/synthetic/c.sock')

    def test_missing_bootstrap_authorization_refuses_before_key_read(self):
        from app.submission_collector import configured_server
        with patch('app.submission_collector.os.open') as opened:
            with self.assertRaises(ProviderUnavailable):configured_server(endpoint='/run/synthetic/c.sock',client_uid=2001,client_gid=2002,ledger_path='/ledger/db',request_key_file='/keys/a',response_key_file='/keys/b',root='/source',target=None,owner_uid=2003,generation=1)
            opened.assert_not_called()


class PersistentRPCTests(unittest.TestCase):
    def setUp(self):
        import test_submission_rpc as fixtures
        fixtures.RPCTests.setUp(self)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'ledger.sqlite'
        self.server.ledger=SQLiteReplayLedger.provision(self.path,guard=fake_guard)

    def test_authenticated_request_is_durable(self):
        self.client.collect(self.request)
        reopened=SQLiteReplayLedger(self.path,guard=fake_guard)
        with self.assertRaises(ProviderUnavailable):reopened.claim(self.request.submission_id)
        with sqlite3.connect(self.path,factory=ClosingConnection) as db:
            self.assertEqual(db.execute('SELECT state FROM requests').fetchone(),('COMPLETE',))

    def test_failure_after_claim_cannot_recollect_after_restart(self):
        def fail(*args):raise RuntimeError('synthetic failure')
        self.server.sources[self.target]=(1,fail)
        with self.assertRaises(ProviderUnavailable):self.client.collect(self.request)
        reopened=SQLiteReplayLedger(self.path,guard=fake_guard)
        with self.assertRaises(ProviderUnavailable):reopened.claim(self.request.submission_id)
        with sqlite3.connect(self.path,factory=ClosingConnection) as db:
            self.assertEqual(db.execute('SELECT state FROM requests').fetchone(),('UNKNOWN',))
