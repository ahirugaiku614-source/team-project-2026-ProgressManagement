"""workerの予約・監視・所有確認・後片付けを、合成Engineと一時SQLiteだけで試験する。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import ast
import hashlib
import hmac
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from execution_worker.api import create_app
from execution_worker.auth import Authenticator
from execution_worker.contracts import Busy, Denied, Frame, Unavailable
from execution_worker.engine import Driver, specification
from execution_worker.registry import Registry
from execution_worker.service import Service
from execution_worker_fakes import Clock, FakeEngine, IMAGE, KEY, command, crash_process, driver, reserve_process, signed


class WorkerTests(unittest.TestCase):
    """実行結果は合成値。Docker/gVisorの隔離・5秒停止を実測した試験ではない。"""

    def setUp(self):
        """元DB/volumeとは異なる一時directoryと合成clock/Engineを作る。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "worker.sqlite"
        self.registry = Registry.create_new(self.path)
        self.clock = Clock()
        self.engine = FakeEngine(self.clock)
        self.service = self.make_service()

    def make_service(self):
        """別接続の管理者でも同じ台帳に接続する構成を再現する。"""
        return Service(Registry(self.path), Authenticator(KEY), driver(self.engine), wall_ms=lambda: 1000000, monotonic_ms=self.clock)

    def run_job(self, request=None, service=None):
        """専用peerと署名を合成し、実ネットワークを経由せずworker契約へ渡す。"""
        return (service or self.service).handle(*signed(request or command()), trusted_peer=True)

    def state(self, request):
        """台帳状態を照合し、UNKNOWNの保持や再送を確認する。"""
        return self.registry.get(request.execution_id)["state"]

    def test_normal_completion_returns_stdout_exit_and_time_after_removal(self):
        request = command(code="raise AssertionError('never-evaluated-on-host')")
        reply = self.run_job(request)
        self.assertEqual(reply.result.status, "completed")
        self.assertEqual(reply.result.stdout, "Hello\n")
        self.assertEqual(reply.result.exit_code, 0)
        self.assertGreaterEqual(reply.result.execution_time_ms, 0)
        self.assertEqual(self.engine.sources, [request.code.encode()])
        self.assertFalse(self.engine.rows)
        self.assertEqual(self.state(request), "COMPLETED")
        self.assertEqual([name for name, _ in self.engine.events], ["create", "attach", "start", "remove"])

    def test_python_exception_and_nonzero_exit_are_forwarded(self):
        self.engine.mode = "error"
        reply = self.run_job()
        self.assertEqual(reply.result.status, "error")
        self.assertEqual(reply.result.exit_code, 7)
        self.assertIn("ValueError", reply.result.stderr)
        self.assertFalse(self.engine.rows)

    def test_timeout_kills_only_owned_id_and_removes_it(self):
        self.engine.mode = "timeout"
        reply = self.run_job()
        self.assertEqual(reply.result.status, "timeout")
        self.assertIsNone(reply.result.exit_code)
        self.assertGreaterEqual(reply.result.execution_time_ms, 5000)
        self.assertEqual([name for name, _ in self.engine.events][-2:], ["kill", "remove"])
        self.assertEqual(len({cid for _, cid in self.engine.events}), 1)
        self.assertFalse(self.engine.rows)

    def test_output_overflow_is_bounded_and_stopped(self):
        self.engine.mode = "overflow"
        reply = self.run_job()
        self.assertEqual(reply.result.status, "error")
        self.assertIsNone(reply.result.exit_code)
        self.assertEqual(len(reply.result.stdout.encode()), 65536)
        self.assertIn("出力サイズ", reply.result.stderr)
        self.assertFalse(self.engine.rows)

    def test_invalid_output_utf8_does_not_expand_public_limit(self):
        self.engine.mode = "bad_utf8"
        reply = self.run_job()
        self.assertLessEqual(len(reply.result.stdout.encode()), 65536)
        self.assertFalse(self.engine.rows)

    def test_fixed_spec_rejects_host_access_and_enforces_requested_limits(self):
        spec = specification(IMAGE, {})
        host = spec["HostConfig"]
        self.assertEqual((host["Runtime"], host["NetworkMode"]), ("runsc", "none"))
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertFalse(host["Privileged"])
        self.assertEqual(spec["User"], "1000:1000")
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertEqual(host["SecurityOpt"], ["no-new-privileges:true"])
        self.assertEqual(host["NanoCpus"], 500000000)
        self.assertEqual(host["Memory"], 134217728)
        self.assertEqual(host["MemorySwap"], host["Memory"])
        self.assertEqual(host["PidsLimit"], 32)
        self.assertEqual(host["ShmSize"], 8 * 1024**2)
        self.assertEqual(host["Binds"], [])
        self.assertEqual(host["Mounts"], [])
        self.assertEqual(host["Devices"], [])
        self.assertEqual(host["LogConfig"]["Type"], "none")
        self.assertFalse(host["AutoRemove"])
        self.assertEqual(set(host["Tmpfs"]), {"/tmp", "/work"})

    def test_runsc_missing_never_creates_or_falls_back(self):
        self.engine.runsc_verified = False
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertFalse(self.engine.events)
        self.assertEqual(self.state(request), "COMPLETED")

    def test_default_engine_and_unknown_image_never_connect(self):
        for selected in (Driver(), Driver(IMAGE)):
            service = Service(self.registry, Authenticator(KEY), selected, wall_ms=lambda: 1000000)
            with self.assertRaises(Unavailable):
                self.run_job(service=service)

    def test_four_running_jobs_are_shared_across_clients_and_fifth_rejected(self):
        self.engine.block = True
        services = [self.make_service() for _ in range(5)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.run_job, command(user=index + 1), services[index]) for index in range(4)]
            try:
                self.assertTrue(self.engine.four.wait(5))
                with self.assertRaises(Busy):
                    self.run_job(command(user=5), services[4])
                self.assertEqual(len(self.engine.rows), 4)
            finally:
                self.engine.release.set()
            self.assertTrue(all(future.result(5).result.status == "completed" for future in futures))
        self.assertFalse(self.engine.rows)

    def test_distinct_api_clients_observe_same_worker_capacity(self):
        from app.execution_client import ExecutionBusy, ExecutionClient, WorkerUnavailable
        service = self.service
        class SignedMockClient:
            """別API processのクライアントを模擬する。実ネットワーク接続はしない。"""
            def execute(self, request):
                """worker満枠をAPIの429へ対応付ける契約を検査する。"""
                try:
                    return service.handle(*signed(request), trusted_peer=True)
                except Busy:
                    raise ExecutionBusy() from None
                except (Denied, Unavailable):
                    raise WorkerUnavailable() from None
        clients = [ExecutionClient(SignedMockClient()) for _ in range(5)]
        self.engine.block = True
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(clients[index].execute, command(user=index + 1)) for index in range(4)]
            try:
                self.assertTrue(self.engine.four.wait(5))
                with self.assertRaises(ExecutionBusy):
                    clients[4].execute(command(user=5))
            finally:
                self.engine.release.set()
            self.assertTrue(all(future.result(5).status == "completed" for future in futures))

    def test_same_execution_id_replays_result_without_second_create(self):
        request = command()
        first = self.run_job(request)
        second = self.run_job(request)
        self.assertEqual(first, second)
        self.assertEqual(sum(name == "create" for name, _ in self.engine.events), 1)
        with self.assertRaises(Denied):
            self.run_job(request.model_copy(update={"code": "different"}))

    def test_concurrent_duplicate_and_recovery_cannot_change_running_job(self):
        request = command()
        self.engine.block = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.run_job, request)
            try:
                self.assertTrue(self.engine.entered.wait(5))
                with self.assertRaises(Unavailable):
                    self.run_job(request, self.make_service())
                with self.assertRaises(Unavailable):
                    self.make_service().recover(request.execution_id)
                self.assertEqual(self.state(request), "RUNNING")
                self.assertEqual(sum(name == "create" for name, _ in self.engine.events), 1)
            finally:
                self.engine.release.set()
            self.assertEqual(future.result(5).result.status, "completed")

    def test_signature_nonce_replay_is_rejected_after_process_restart(self):
        request = command()
        envelope = signed(request)
        self.service.handle(*envelope, trusted_peer=True)
        with self.assertRaises(Denied):
            self.make_service().handle(*envelope, trusted_peer=True)

    def test_authorization_expiring_during_database_wait_never_creates(self):
        self.service.wall_ms = unittest.mock.Mock(side_effect=[1000000, 1000000, 1005001])
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertFalse(self.engine.events)
        self.assertEqual(self.state(request), "COMPLETED")

    def test_worker_rejects_oversized_source_before_any_engine_operation(self):
        request = command().model_copy(update={"code": "a" * 65537})
        with self.assertRaises(Denied):
            self.run_job(request)
        self.assertFalse(self.engine.events)

    def test_invalid_signature_expiry_untrusted_peer_and_changed_owner_rejected(self):
        request = command()
        body, timestamp, nonce, signature = signed(request)
        for values in ((body, timestamp, nonce, "0" * 64), (body, "990000", nonce, signature),
                       (body, timestamp, "not-a-uuid", signature), (body.replace(b'"user_id":1', b'"user_id":9'), timestamp, nonce, signature)):
            with self.subTest(values=values[1:]), self.assertRaises(Denied):
                self.service.handle(*values, trusted_peer=True)
        with self.assertRaises(Denied):
            self.service.handle(body, timestamp, nonce, signature, trusted_peer=False)
        self.assertFalse(self.engine.events)

    def test_create_response_lost_remains_unknown_without_guessing_id(self):
        self.engine.fail_create = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        self.assertIsNone(self.registry.get(request.execution_id)["container_id"])
        self.assertFalse(self.service.recover(request.execution_id))
        self.assertEqual(len(self.engine.rows), 1)
        with self.assertRaises(Busy):
            self.run_job()
        self.assertFalse(any(name in ("kill", "remove") for name, _ in self.engine.events))

    def test_stream_failure_is_unknown_and_explicit_recovery_cleans_known_id(self):
        self.engine.fail_read = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        self.engine.fail_read = False
        self.assertTrue(self.make_service().recover(request.execution_id))
        self.assertEqual(self.state(request), "COMPLETED")
        self.assertFalse(self.engine.rows)

    def test_kill_failure_retains_slot_and_refuses_new_work(self):
        self.engine.mode, self.engine.fail_kill = "timeout", True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        with self.assertRaises(Busy):
            self.run_job()
        self.assertFalse(any(name == "remove" for name, _ in self.engine.events))
        self.engine.fail_kill = False
        self.assertTrue(self.service.recover(request.execution_id))

    def test_kill_reply_loss_recovers_without_second_kill(self):
        self.engine.mode, self.engine.kill_lost = "timeout", True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        self.assertTrue(self.service.recover(request.execution_id))
        self.assertEqual(sum(name == "kill" for name, _ in self.engine.events), 1)

    def test_unconfirmed_stop_never_proceeds_to_remove(self):
        self.engine.mode, self.engine.keep_running = "timeout", True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        self.assertFalse(any(name == "remove" for name, _ in self.engine.events))

    def test_signed_duplicate_json_key_is_rejected(self):
        body, timestamp, nonce, _ = signed(command())
        body = body[:-1] + b',"code":"different"}'
        message = f"v1\n{timestamp}\n{nonce}\n".encode() + hashlib.sha256(body).hexdigest().encode()
        signature = hmac.new(KEY, message, hashlib.sha256).hexdigest()
        with self.assertRaises(Denied):
            self.service.handle(body, timestamp, nonce, signature, trusted_peer=True)
        self.assertFalse(self.engine.events)

    def test_remove_failure_and_unconfirmed_remove_retain_unknown(self):
        for attribute in ("fail_remove", "keep_removed"):
            with self.subTest(attribute=attribute):
                request = command()
                setattr(self.engine, attribute, True)
                with self.assertRaises(Unavailable):
                    self.run_job(request)
                self.assertEqual(self.state(request), "UNKNOWN")
                setattr(self.engine, attribute, False)
                self.assertTrue(self.service.recover(request.execution_id))
        self.assertFalse(self.engine.rows)

    def test_wrong_owner_runtime_or_mount_during_inspect_is_never_killed(self):
        for section, key, value in (("Labels", "user_id", "999"), ("HostConfig", "Runtime", "runc"),
                                    ("HostConfig", "Mounts", [{"Source": "/host"}])):
            with self.subTest(key=key):
                request = command()
                original = self.engine.create
                def tamper(name, spec):
                    """作成結果の所有者やruntimeがすり替わった故障を再現する。"""
                    cid = original(name, spec)
                    self.engine.rows[cid]["spec"][section][key] = value
                    return cid
                with patch.object(self.engine, "create", side_effect=tamper), self.assertRaises(Unavailable):
                    self.run_job(request)
                with self.assertRaises(Unavailable):
                    self.service.recover(request.execution_id)
                self.assertEqual(self.state(request), "UNKNOWN")
                self.assertFalse(any(name in ("kill", "remove") for name, _ in self.engine.events))
                # UNKNOWNを含む各ケースは別の専用試験台帳で実施する。
                self.path = Path(self.temp.name) / (str(uuid4()) + ".sqlite")
                self.registry = Registry.create_new(self.path)
                self.engine = FakeEngine(self.clock)
                self.service = self.make_service()

    def test_short_or_other_container_id_is_rejected(self):
        for cid in ("short", "a" * 64):
            with patch.object(self.engine, "create", return_value=cid), self.assertRaises(Unavailable):
                self.run_job()
            self.assertFalse(any(name in ("kill", "remove", "start") for name, _ in self.engine.events))
            # 2例目を別台帳にし、UNKNOWNの全体受付停止を迂回して試験しない。
            self.path = Path(self.temp.name) / (str(uuid4()) + ".sqlite")
            self.registry = Registry.create_new(self.path)
            self.service = self.make_service()

    def test_database_failure_before_reservation_never_calls_create(self):
        with self.registry.connection() as db:
            db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECRET'); END")
        with self.assertRaises(Unavailable):
            self.run_job()
        self.assertFalse(self.engine.events)

    def test_database_failure_after_removal_recovers_saved_result(self):
        request = command()
        with self.registry.connection() as db:
            db.execute("CREATE TRIGGER fail_done BEFORE UPDATE ON jobs WHEN NEW.state='COMPLETED' BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECRET'); END")
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        self.assertFalse(self.engine.rows)
        with self.registry.connection() as db:
            db.execute("DROP TRIGGER fail_done")
        restored = self.make_service()
        self.assertTrue(restored.recover(request.execution_id))
        self.assertEqual(self.run_job(request, restored).result.stdout, "Hello\n")
        self.assertEqual(sum(name == "create" for name, _ in self.engine.events), 1)

    def test_registry_rejects_stale_revision_and_different_daemon(self):
        request = command()
        row, _ = self.registry.admit(request, str(uuid4()), 1000000, specification(IMAGE, {}))
        self.registry.advance(row, "CREATING")
        with self.assertRaises(Unavailable):
            self.registry.advance(row, "UNKNOWN")
        self.registry.bind("test-profile")
        with self.assertRaises(Unavailable):
            Registry(self.path).bind("different-profile")

    def test_recovery_refuses_different_daemon_before_inspecting_or_removing(self):
        request = command()
        self.engine.fail_remove = True
        with self.assertRaises(Unavailable):
            self.run_job(request)
        other = Service(Registry(self.path), Authenticator(KEY), Driver(IMAGE, self.engine, "other-daemon"))
        with patch.object(self.engine, "inspect") as inspected, self.assertRaises(Unavailable):
            other.recover(request.execution_id)
        inspected.assert_not_called()
        self.assertEqual(self.state(request), "UNKNOWN")
        self.engine.fail_remove = False
        self.assertTrue(self.service.recover(request.execution_id))

    def test_existing_file_is_never_overwritten(self):
        before = self.path.read_bytes()
        with self.assertRaises(Unavailable):
            Registry.create_new(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_multiprocess_atomic_four_reservations_survive_reopen(self):
        ctx = multiprocessing.get_context("spawn")
        barrier, queue = ctx.Barrier(8), ctx.Queue()
        processes = [ctx.Process(target=reserve_process, args=(str(self.path), barrier, queue)) for _ in range(8)]
        try:
            for process in processes:
                process.start()
            outcomes = [queue.get(timeout=40) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(outcomes.count("reserved"), 4)
            self.assertEqual(outcomes.count("Busy"), 4)
            with Registry(self.path).connection() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE state!='COMPLETED'").fetchone()[0], 4)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()  # 自分が作ったSQLite試験用Python子processだけ。
                process.join(5)
            queue.close()

    def test_process_death_releases_lock_but_not_uncertain_reservation(self):
        request = command()
        process = multiprocessing.get_context("spawn").Process(target=crash_process, args=(str(self.path), str(request.execution_id)))
        process.start()
        try:
            process.join(15)
            self.assertEqual(process.exitcode, 23)
            self.assertEqual(self.state(request), "CREATING")
            self.assertFalse(self.make_service().recover(request.execution_id))
            self.assertEqual(self.state(request), "UNKNOWN")
            with self.assertRaises(Busy):
                self.run_job()
            self.assertFalse(self.engine.events)
        finally:
            if process.is_alive():
                process.terminate()
            process.join(5)

    def test_default_worker_http_disabled_and_peer_header_cannot_bypass_auth(self):
        with TestClient(create_app()) as client:
            self.assertEqual(client.post("/internal/execute", json={}).status_code, 503)
        with TestClient(create_app(self.service, lambda scope: False)) as client:
            response = client.post("/internal/execute", headers={"x-peer-verified": "true"}, json={})
            self.assertEqual(response.status_code, 401)
        self.assertFalse(self.engine.events)

    def test_worker_http_requires_signature_and_returns_bound_result(self):
        body, timestamp, nonce, signature = signed(command())
        headers = {"x-execution-timestamp": timestamp, "x-execution-nonce": nonce, "x-execution-signature": signature}
        with TestClient(create_app(self.service, lambda scope: True)) as client:
            response = client.post("/internal/execute", content=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["result"]["stdout"], "Hello\n")
            self.assertEqual(client.post("/internal/execute", content=body, headers=headers).status_code, 401)
            self.assertEqual(client.post("/internal/execute", content=b"{}", headers=headers).status_code, 401)

    def test_http_input_limit_and_invalid_signatures_do_not_expose_body(self):
        with TestClient(create_app(self.service, lambda scope: True)) as client:
            for body in (b"SYNTHETIC_SECRET", b"x" * (512 * 1024 + 1)):
                response = client.post("/internal/execute", content=body)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("SYNTHETIC_SECRET", response.text)
        self.assertFalse(self.engine.events)

    def test_default_entrypoint_never_starts_service_or_engine(self):
        from execution_worker.__main__ import main
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(), 2)
        self.assertFalse(json.loads(output.getvalue())["launch_enabled"])
        self.assertEqual(json.loads(output.getvalue())["docker"], "NOT_RUN")

    def test_worker_has_no_live_transport_or_host_code_evaluation(self):
        root = Path(__file__).resolve().parents[1] / "execution_worker"
        forbidden = {"docker", "subprocess", "socket", "requests", "httpx", "urllib", "uvicorn", "libvirt"}
        for path in root.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name.split(".")[0] in forbidden for alias in node.names))
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module.split(".")[0], forbidden)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"exec", "eval", "compile", "__import__"})
