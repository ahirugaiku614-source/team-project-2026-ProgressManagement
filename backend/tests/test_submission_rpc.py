"""実socketを使わず、固定合成入力と偽通信でRPCの拒否条件を検証する。"""
from dataclasses import replace
from pathlib import Path
import json
import threading
import struct
import tempfile
import subprocess
import sys
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_projects  # 実DB設定を使わずモデルを読み込む既存fixture。
from app.submission_rpc import (
    RPCKeys, CollectorRPCClient, CollectorRPCServer, MemoryReplayLedger,
    DeniedRPCTransport, pack, unpack, MAX_REQUEST, MAX_RESPONSE, decode_files,
)
from app.submission_provider import ProviderUnavailable, SubmissionCollection, get_submission_provider, DeniedSubmissionProvider
from app.submission_targets import SubmissionTarget
from app.submission_lab_plan import validate_lab_plan


class FakeClock:
    def __init__(self): self.wall = 1000; self.mono = 1.0
    def now(self): return self.wall
    def monotonic(self): return self.mono


class FakeTransport:
    """パケットはメモリ内だけ。故障注入でも実通信へ切り替えない。"""
    def __init__(self, server):
        self.server = server; self.calls = 0
        self.before = lambda frame: frame
        self.after = lambda frame: frame
    def exchange(self, frame, *, timeout, max_response):
        self.calls += 1
        return self.after(self.server.handle(self.before(frame)))


class RPCTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        # テスト専用の公開固定値。本番鍵として使用してはいけない。
        self.keys = RPCKeys(b'test-request-key-' * 2, b'test-response-key' * 2)
        self.target = SubmissionTarget(1, 2, 3, 4, 'assignment-1')
        self.request = SubmissionCollection(str(uuid4()), str(uuid4()), str(uuid4()), self.target)
        self.collected = []
        def collect(target, session):
            self.collected.append((target, session))
            return {'main.txt': ('100644', b'fixed synthetic data\n')}
        self.server = CollectorRPCServer(self.keys, MemoryReplayLedger(), {self.target: (1, collect)},
                                         lambda r: True, self.clock.now)
        self.transport = FakeTransport(self.server)
        self.client = CollectorRPCClient(self.keys, MemoryReplayLedger(), lambda t: 1,
            self.transport, self.clock.now, self.clock.monotonic)

    def rewrite_request(self, change):
        def before(frame):
            value = unpack(frame, self.keys.request_key, b'REQUEST\0', MAX_REQUEST)
            change(value)
            return pack(value, self.keys.request_key, b'REQUEST\0', MAX_REQUEST)
        self.transport.before = before

    def rewrite_response(self, change):
        def after(frame):
            value = unpack(frame, self.keys.response_key, b'RESPONSE\0', MAX_RESPONSE)
            change(value)
            return pack(value, self.keys.response_key, b'RESPONSE\0', MAX_RESPONSE)
        self.transport.after = after

    def test_normal_fixed_request_and_memory_bytes(self):
        self.assertEqual(self.client.collect(self.request), {'main.txt': ('100644', b'fixed synthetic data\n')})
        self.assertEqual(self.collected, [(self.target, self.request.editing_session_id)])

    def test_request_authentication_failure_collects_nothing(self):
        self.client.keys = RPCKeys(b'wrong-client-key-' * 2, self.keys.response_key)
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
        self.assertEqual(self.collected, [])

    def test_response_authentication_failure_no_retry(self):
        self.client.keys = RPCKeys(self.keys.request_key, b'wrong-response-key' * 2)
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
        self.assertEqual(self.transport.calls, 1)

    def test_authorization_denied_collects_nothing(self):
        self.server.authorize = lambda r: False
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
        self.assertEqual(self.collected, [])

    def test_student_a_cannot_select_student_b(self):
        request = replace(self.request, target=replace(self.target, student_id=5))
        with self.assertRaises(ProviderUnavailable): self.client.collect(request)
        self.assertEqual(self.collected, [])

    def test_unregistered_task_rejected(self):
        request = replace(self.request, target=replace(self.target, task_id=99))
        with self.assertRaises(ProviderUnavailable): self.client.collect(request)

    def test_unknown_path_and_url_fields_rejected(self):
        for field in ('absolute_path', 'url'):
            with self.subTest(field=field):
                self.setUp()
                self.rewrite_request(lambda r: r.update({field: '/private'}))
                with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
                self.assertEqual(self.collected, [])

    def test_traversal_path_rejected(self):
        self.rewrite_request(lambda r: r['target'].update(folder_path='../other'))
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)

    def test_source_generation_mismatch(self):
        self.client.generation_for = lambda t: 2
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
        self.assertEqual(self.collected, [])

    def test_expired_request_rejected(self):
        self.rewrite_request(lambda r: r.update(issued_at=800, expires_at=900))
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)

    def test_future_request_rejected(self):
        self.rewrite_request(lambda r: r.update(issued_at=1100, expires_at=1190))
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)

    def test_response_binding_mismatch(self):
        self.rewrite_response(lambda r: r['binding'].update(request_id=str(uuid4())))
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)

    def test_dirty_or_unverified_session_rejected(self):
        self.rewrite_response(lambda r: r.update(saved_files_confirmed=False))
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)

    def test_partial_frame_disconnect_no_resend(self):
        self.transport.after = lambda frame: frame[:-1]
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(len(self.collected), 1)

    def test_transport_timeout_no_resend(self):
        def timeout(frame): raise TimeoutError()
        self.transport.before = timeout
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)
        self.assertEqual(self.transport.calls, 1)

    def test_late_response_rejected(self):
        def late(frame): self.clock.mono += 6; return frame
        self.transport.after = late
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)

    def test_expiry_during_collection_rejected(self):
        original = self.server.authorize
        calls = []
        def auth(r):
            calls.append(1)
            if len(calls) == 1: self.clock.wall += 120
            return original(r)
        self.server.authorize = auth
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)

    def test_duplicate_with_new_nonce_rejected_on_server(self):
        frames = []
        self.transport.before = lambda frame: frames.append(frame) or frame
        self.client.collect(self.request)
        with self.assertRaises(ProviderUnavailable): self.server.handle(frames[0])
        self.assertEqual(len(self.collected), 1)

    def test_parallel_claim_is_single_use(self):
        ledger = MemoryReplayLedger(); results = []
        def claim():
            try: ledger.claim('operation'); results.append(True)
            except ProviderUnavailable: results.append(False)
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(results, [True, False])

    def test_response_size_limit(self):
        self.transport.after = lambda f: b'x' * (MAX_RESPONSE + 5)
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)

    def test_duplicate_json_keys_and_extra_frames(self):
        raw = b'{"payload":{},"payload":{},"mac":"x"}'
        with self.assertRaises(ProviderUnavailable): unpack(len(raw).to_bytes(4,'big') + raw, self.keys.request_key, b'REQUEST\0', MAX_REQUEST)
        self.transport.after = lambda f: f + f
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)

    def test_duplicate_file_and_secret_paths_rejected(self):
        entries = [{'path':'main.txt','mode':'100644','data':'eA=='}]
        with self.assertRaises(ProviderUnavailable): decode_files(entries * 2)
        for name in ('.env', '../outside', '/absolute', 'secret.key'):
            with self.assertRaises(ProviderUnavailable): decode_files([{**entries[0], 'path':name}])

    def test_file_directory_collision_rejected(self):
        with self.assertRaises(ProviderUnavailable): decode_files([
            {'path':'a','mode':'100644','data':'eA=='}, {'path':'a/b','mode':'100644','data':'eA=='}])

    def test_default_transport_and_provider_remain_denied(self):
        with self.assertRaises(ProviderUnavailable): DeniedRPCTransport().exchange(b'',timeout=1,max_response=1)
        self.assertIsInstance(get_submission_provider(), DeniedSubmissionProvider)

    def test_no_secret_in_key_repr(self):
        self.assertNotIn('test-request', repr(self.keys))

    def test_noninteger_generation_rejected(self):
        self.client.generation_for = lambda t: True
        with self.assertRaises(ProviderUnavailable): self.client.collect(self.request)


class LabPlanTests(unittest.TestCase):
    def setUp(self):
        self.values = {}
        for i, name in enumerate(('STUDENT_A','STUDENT_B','COLLECTOR_A','COLLECTOR_B','MANAGER','POSTGRES'), 1):
            self.values[name+'_UID'] = 2000+i; self.values[name+'_GID'] = 2000+i
        for name in ('STUDENT_A_PROJECT_ROOT','STUDENT_B_PROJECT_ROOT','COLLECTOR_A_SOCKET_DIR',
                     'COLLECTOR_B_SOCKET_DIR','COLLECTOR_A_LEDGER_DIR','COLLECTOR_B_LEDGER_DIR',
                     'COLLECTOR_A_KEYS_DIR','COLLECTOR_B_KEYS_DIR','MANAGED_GIT_ROOT','SUBMISSION_DB_ROOT',
                     'STUDENT_A_CONFIG','STUDENT_B_CONFIG','MANAGER_FIREBASE_CREDENTIAL_FILE'):
            self.values[name] = '/srv/synthetic-submission/' + name.lower()
        for name in ('SUBMISSION_CODE_IMAGE','SUBMISSION_MANAGER_IMAGE','SUBMISSION_POSTGRES_IMAGE'):
            self.values[name] = 'example.invalid/synthetic@sha256:' + 'a'*64
        self.values.update(STUDENT_A_PORT=18081, STUDENT_B_PORT=18082)

    def test_valid_metadata_plan(self):
        self.assertTrue(validate_lab_plan(self.values, '/srv/synthetic-submission'))

    def test_root_or_shared_student_identity_rejected(self):
        for value in (0, self.values['STUDENT_A_UID']):
            self.values['STUDENT_B_UID'] = value
            with self.assertRaises(ValueError): validate_lab_plan(self.values, '/srv/synthetic-submission')

    def test_shared_or_outside_paths_rejected(self):
        for path in (self.values['STUDENT_A_PROJECT_ROOT'], '/home/example-user/outside-workspace'):
            self.values['STUDENT_B_PROJECT_ROOT'] = path
            with self.assertRaises(ValueError): validate_lab_plan(self.values, '/srv/synthetic-submission')

    def test_unpinned_image_rejected(self):
        self.values['SUBMISSION_CODE_IMAGE'] = 'example.invalid/synthetic:latest'
        with self.assertRaises(ValueError): validate_lab_plan(self.values, '/srv/synthetic-submission')

    def test_compose_isolated_services_without_docker_execution(self):
        import yaml
        path = Path(__file__).parents[2] / 'labs/saved-submission/compose.yaml'
        plan = yaml.safe_load(path.read_text(encoding='utf-8'))
        services = plan['services']
        self.assertEqual(set(services), {'student-a','student-b','collector-a','collector-b','manager','postgres'})
        for name in ('student-a','student-b','collector-a','collector-b','manager','postgres'):
            config = services[name]
            self.assertTrue(config['read_only'])
            self.assertEqual(config['cap_drop'], ['ALL'])
            self.assertIn('no-new-privileges:true', config['security_opt'])
            self.assertIn('profiles', config)
            self.assertNotIn('privileged', config)
        for name in ('collector-a','collector-b'):
            self.assertEqual(services[name]['network_mode'], 'none')
            self.assertNotIn('ports', services[name])
            self.assertTrue(services[name]['volumes'][0]['read_only'])
        for name in ('student-a','student-b'):
            mounts=services[name]['volumes']
            self.assertEqual({m['target'] for m in mounts}, {'/workspace','/etc/code-server/config.yaml'})
            self.assertTrue(all(m['bind']['create_host_path'] is False for m in mounts))
        self.assertNotIn('ports', services['postgres'])

    def test_collector_contract_imports_without_db_or_credentials(self):
        # 独立Pythonでキャッシュを排除し、DB設定なしでも純粋な収集契約を読めることを確認。
        env={k:v for k,v in os.environ.items() if not k.startswith(('POSTGRES_', 'DATABASE_', 'PROJECT_TEST_'))}
        env['PYTHONDONTWRITEBYTECODE']='1'
        script="import sys; import app.submission_rpc; import app.submission_uds; assert 'app.models' not in sys.modules; assert 'app.firebase' not in sys.modules"
        result=subprocess.run([sys.executable,'-B','-c',script],env=env,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0)


class UDSContractTests(unittest.TestCase):
    """Linux socket APIはすべて偽物。実Unix socketをWindowsから使わない。"""
    def setUp(self):
        from app.submission_uds import UnixSocketTransport
        self.transport = UnixSocketTransport('/run/synthetic/collector.sock', 3001, enabled=True)
        self.peer = 3001; self.wire = b'\x00\x00\x00\x02ok'; self.sent = []
        case = self
        class Socket:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, seconds): case.assertTrue(0 < seconds <= 1)
            def connect(self, endpoint): case.assertEqual(endpoint, '/run/synthetic/collector.sock')
            def getsockopt(self, *args): return struct.pack('3i', 100, case.peer, 3001)
            def sendall(self, frame): case.sent.append(frame)
            def shutdown(self, direction): pass
            def recv(self, count):
                # 一バイトずつ返し、ヘッダー/本文の部分受信を確実に通す。
                result = case.wire[:1]; case.wire = case.wire[1:]; return result
        self.stack = []
        for target, value in [('app.submission_uds.sys.platform','linux'),
                              ('app.submission_uds.check_endpoint',lambda *args:(1,2,3001,0o660)),
                              ('app.submission_uds.socket.socket',lambda *args:Socket())]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        # Windowsのsocket moduleにはSO_PEERCREDがないため、定数も偽値として注入する。
        p=patch('app.submission_uds.socket.SO_PEERCRED', 17, create=True);p.start();self.addCleanup(p.stop)
        p=patch('app.submission_uds.socket.AF_UNIX', 1, create=True);p.start();self.addCleanup(p.stop)

    def test_fragmented_response_and_peer_credentials(self):
        self.assertEqual(self.transport.exchange(b'12345',timeout=1,max_response=100), b'\x00\x00\x00\x02ok')
        self.assertEqual(self.sent, [b'12345'])

    def test_wrong_peer_uid_rejected_before_send(self):
        self.peer = 999
        with self.assertRaises(ProviderUnavailable): self.transport.exchange(b'12345',timeout=1,max_response=100)
        self.assertEqual(self.sent, [])

    def test_partial_eof_rejected(self):
        self.wire = b'\x00\x00\x00\x02o'
        with self.assertRaises(ProviderUnavailable): self.transport.exchange(b'12345',timeout=1,max_response=100)

    def test_excess_and_oversized_responses_rejected(self):
        for value in (b'\x00\x00\x00\x02okextra', b'\x7f\xff\xff\xff'):
            self.wire = value
            with self.assertRaises(ProviderUnavailable): self.transport.exchange(b'12345',timeout=1,max_response=100)

    def test_disabled_transport_does_not_create_socket(self):
        from app.submission_uds import UnixSocketTransport
        with patch('app.submission_uds.socket.socket') as factory:
            with self.assertRaises(ProviderUnavailable): UnixSocketTransport('/run/synthetic/c.sock',3001).exchange(b'12345',timeout=1,max_response=100)
            factory.assert_not_called()

    def test_deadline_expiry_rejected(self):
        times = iter([1,3]); self.transport.monotonic = lambda:next(times)
        with self.assertRaises(ProviderUnavailable): self.transport.exchange(b'12345',timeout=1,max_response=100)


class RPCGitTests(unittest.TestCase):
    """RPCは偽物、保存先Gitだけ隔離された一時リポジトリを使う。"""
    def setUp(self):
        RPCTests.setUp(self)
        from submission_snapshot_fakes import PortableGitFixture
        from app.submission_git import GitSnapshotStore, ManagedSnapshotProvider
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.git=PortableGitFixture(self.temp.name)
        self.provider=ManagedSnapshotProvider(None,GitSnapshotStore(self.git.session),lambda r:True,
            lambda r:True,collector=self.client.collect)

    def test_rpc_collection_saved_in_manager_git(self):
        receipt=self.provider.collect(self.request)
        self.assertEqual(set(self.git.entries(receipt.commit_id)), {'main.txt'})
        self.provider.verify(self.request,receipt)
        self.assertEqual(len(self.collected),1)

    def test_git_failure_never_recollects_over_rpc(self):
        self.git.failure='ref'
        with self.assertRaises(Exception):self.provider.collect(self.request)
        self.git.failure=None
        with self.assertRaises(Exception):self.provider.collect(self.request)
        self.assertEqual(len(self.collected),1)
        with self.assertRaises(Exception):self.provider.recover(self.request)
