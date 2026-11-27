"""PR #17追補：異常時停止、再起動検出、容量保守、I/O部分失敗を合成データで検証する。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_execution_worker as base
from execution_worker.contracts import Busy, Denied, Frame, Unavailable
from execution_worker.engine import specification
from execution_worker_fakes import IMAGE, command, signed


class WorkerReviewTests(unittest.TestCase):
    """既存fixtureを再利用するが、既存37件を重複収集せず追加境界だけを試験する。"""

    setUp = base.WorkerTests.setUp
    make_service = base.WorkerTests.make_service
    run_job = base.WorkerTests.run_job
    state = base.WorkerTests.state

    def orphan_running(self):
        """lock管理者が消えたRUNNINGと合成containerを作る。実Engineは呼ばない。"""
        request = command()
        spec = specification(IMAGE, {"owner": "synthetic-only", "execution_id": str(request.execution_id)})
        self.registry.bind(self.service.driver.profile())
        row, _ = self.registry.admit(request, str(uuid4()), 1000000, spec)
        row = self.registry.advance(row, "CREATING")
        cid = self.engine.create("synthetic-orphan", spec)
        row = self.registry.advance(row, "CREATING", container_id=cid)
        self.engine.start(cid)
        self.registry.advance(row, "RUNNING")
        return request, cid

    def test_stream_failure_with_stop_failure_keeps_unknown_and_closes_io(self):
        self.engine.fail_read = self.engine.fail_kill = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        actions = [name for name, _ in self.engine.events]
        self.assertIn("kill", actions)
        self.assertIn("close", actions)
        self.assertNotIn("remove", actions)
        with self.assertRaises(Busy):
            self.run_job()

    def test_stream_failure_with_remove_failure_keeps_evidence_until_recovery(self):
        self.engine.fail_read = self.engine.fail_remove = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        self.assertFalse(next(iter(self.engine.rows.values()))["running"])
        self.engine.fail_remove = False
        self.assertTrue(self.service.recover(request.execution_id))
        self.assertFalse(self.engine.rows)

    def test_owner_changed_before_exception_is_never_killed_or_removed(self):
        request = command()
        def disconnect(cid, timeout_ms, maximum):
            """初回所有確認後に実態が変わった故障を合成する。"""
            self.engine.rows[cid]["spec"]["Labels"]["user_id"] = "other-user"
            raise ConnectionError("SYNTHETIC_SECRET")
        with patch.object(self.engine, "read", side_effect=disconnect), self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        self.assertFalse(any(name in ("kill", "remove") for name, _ in self.engine.events))

    def test_partial_attach_never_starts_and_reclaims_confirmed_container(self):
        self.engine.partial_attach = True
        with self.assertRaises(Unavailable):
            self.run_job()
        self.assertFalse(self.engine.rows)
        self.assertNotIn("start", [name for name, _ in self.engine.events])
        self.assertIn("close", [name for name, _ in self.engine.events])

    def test_start_reply_loss_stops_confirmed_container_without_sending_source(self):
        self.engine.start_lost = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "COMPLETED")
        self.assertFalse(self.engine.sources)
        self.assertEqual([name for name, _ in self.engine.events][-3:], ["kill", "close", "remove"])

    def test_disconnect_after_partial_output_never_returns_success_or_leaks_output(self):
        request = command()
        first = True
        def disconnected(cid, timeout_ms, maximum):
            """出力断片を受け取った直後の接続断を再現する。"""
            nonlocal first
            if first:
                first = False
                return Frame("stdout", b"SYNTHETIC_SECRET")
            raise ConnectionError("SYNTHETIC_SECRET")
        with patch.object(self.engine, "read", side_effect=disconnected), self.assertRaises(Unavailable) as error:
            self.run_job(request)
        self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
        self.assertEqual(self.state(request), "COMPLETED")
        self.assertIsNone(self.registry.get(request.execution_id)["result"])
        self.assertFalse(self.engine.rows)

    def test_partial_send_and_send_disconnect_never_retry_source(self):
        for attribute in ("partial_send", "fail_send", "missing_stdin_close"):
            with self.subTest(attribute=attribute):
                setattr(self.engine, attribute, True)
                before = len(self.engine.sources)
                with self.assertRaises(Unavailable):
                    self.run_job()
                self.assertEqual(len(self.engine.sources), before + 1)
                self.assertFalse(self.engine.rows)
                setattr(self.engine, attribute, False)

    def test_late_attach_and_send_are_failures_without_hidden_retry(self):
        for attribute in ("late_attach", "late_send"):
            with self.subTest(attribute=attribute):
                setattr(self.engine, attribute, True)
                with self.assertRaises(Unavailable):
                    self.run_job()
                self.assertFalse(self.engine.rows)
                setattr(self.engine, attribute, False)

    def test_io_close_failure_retains_unknown_and_saved_result(self):
        self.engine.fail_close = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        self.assertIsNotNone(self.registry.get(request.execution_id)["result"])
        self.assertNotIn("remove", [name for name, _ in self.engine.events])
        self.engine.fail_close = False
        report = self.service.startup_check(recover_owned=True)
        self.assertIn(str(request.execution_id), report["recovered"])
        self.assertEqual(self.run_job(request).result.stdout, "Hello\n")

    def test_eof_without_process_exit_does_not_report_success(self):
        self.engine.eof_running = True
        reply = self.run_job()
        self.assertEqual(reply.result.status, "timeout")
        self.assertFalse(self.engine.rows)

    def test_process_exit_without_confirmed_stream_eof_is_infrastructure_failure(self):
        self.engine.missing_eof = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "COMPLETED")
        self.assertFalse(self.engine.rows)

    def test_invalid_frame_stops_only_owned_container(self):
        for frame in (Frame("stdout", b"x" * 4097), Frame("unknown", b"private"), Frame("eof", b"not-empty")):
            with self.subTest(stream=frame.stream), patch.object(self.engine, "read", return_value=frame), self.assertRaises(Unavailable):
                self.run_job()
            self.assertFalse(self.engine.rows)

    def test_stderr_overflow_is_bounded_and_terminated(self):
        self.engine.mode = "stderr_overflow"
        reply = self.run_job()
        self.assertEqual(reply.result.status, "error")
        self.assertLessEqual(len(reply.result.stderr.encode()), 65536)
        self.assertIn("出力サイズ", reply.result.stderr)
        self.assertFalse(self.engine.rows)

    def test_none_poll_is_not_eof_and_io_budgets_are_explicit(self):
        original = self.engine.read
        first = True
        def delayed(cid, timeout_ms, maximum):
            """初回は未到着、以降は正規frameを返す。"""
            nonlocal first
            if first:
                first = False
                return None
            return original(cid, timeout_ms, maximum)
        with patch.object(self.engine, "read", side_effect=delayed):
            self.assertEqual(self.run_job().result.status, "completed")
        for call in self.engine.io_calls:
            self.assertGreater(call[1], 0)
            self.assertLessEqual(call[1], 50 if call[0] == "read" else 1000)
            if call[0] == "read":
                self.assertEqual(call[2], 4096)

    def test_restart_detects_orphan_without_automatic_engine_mutation(self):
        request, cid = self.orphan_running()
        before = list(self.engine.events)
        restored = self.make_service()
        self.assertIn(str(request.execution_id), restored.startup_report["unresolved"])
        self.assertEqual(self.state(request), "UNKNOWN")
        self.assertEqual(self.engine.events, before)
        self.assertTrue(self.engine.rows[cid]["running"])
        with self.assertRaises(Busy):
            self.run_job(service=restored)
        report = restored.startup_check(recover_owned=True)
        self.assertIn(str(request.execution_id), report["recovered"])
        self.assertFalse(self.engine.rows)

    def test_startup_recovery_owner_mismatch_blocks_new_admissions(self):
        request, cid = self.orphan_running()
        self.engine.rows[cid]["spec"]["User"] = "0:0"
        report = self.service.startup_check(recover_owned=True)
        self.assertIn(str(request.execution_id), report["unresolved"])
        self.assertFalse(any(name in ("kill", "remove") for name, _ in self.engine.events))
        with self.assertRaises(Busy):
            self.run_job()

    def test_restart_and_scan_do_not_take_over_an_active_job(self):
        request = command()
        self.engine.block = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.run_job, request)
            try:
                self.assertTrue(self.engine.entered.wait(5))
                restored = self.make_service()
                self.assertIn(str(request.execution_id), restored.startup_report["active"])
                self.assertIn(str(request.execution_id), restored.startup_check(recover_owned=True)["active"])
                self.assertEqual(self.state(request), "RUNNING")
                self.assertNotIn("kill", [name for name, _ in self.engine.events])
            finally:
                self.engine.release.set()
            self.assertEqual(future.result(5).result.status, "completed")

    def test_capacity_limits_reject_without_growing_rows_or_calling_engine(self):
        for name, limit in (("MAX_JOBS", 0), ("MAX_NONCES", 0), ("MAX_LOCK_FILES", 0), ("MAX_STATE_BYTES", 1), ("MIN_FREE_BYTES", 2**80)):
            with self.subTest(name=name), patch("execution_worker.registry." + name, limit), self.assertRaises(Busy):
                self.run_job()
            report = self.registry.capacity_report()
            self.assertEqual(report["jobs"], 0)
            self.assertEqual(report["nonces"], 0)
            self.assertEqual(report["locks"], 0)
        self.assertFalse(self.engine.events)

    def test_orphan_lock_is_preserved_and_blocks_admission(self):
        lock = Path(str(self.path) + "." + str(uuid4()) + ".lock")
        lock.write_bytes(b"synthetic-lock")
        with self.assertRaises(Busy):
            self.run_job()
        self.assertEqual(self.registry.capacity_report()["orphan_locks"], 1)
        self.assertEqual(self.registry.redact_completed(1000000), 0)
        self.assertEqual(lock.read_bytes(), b"synthetic-lock")

    def test_retention_keeps_unknown_candidate_nonce_and_all_lock_files(self):
        complete = command()
        self.run_job(complete)
        self.engine.fail_remove = True
        unknown = command()
        with self.assertRaises(Unavailable):
            self.run_job(unknown)
        before = self.registry.capacity_report()
        candidate = self.registry.get(unknown.execution_id)["result"]
        files = sorted(path.name for path in self.path.parent.glob("*.lock"))
        self.assertEqual(self.registry.redact_completed(1000000), 1)
        self.assertIsNone(self.registry.get(complete.execution_id)["result"])
        self.assertEqual(self.registry.get(unknown.execution_id)["result"], candidate)
        self.assertEqual(self.state(unknown), "UNKNOWN")
        after = self.registry.capacity_report()
        for key in ("jobs", "nonces", "locks"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(sorted(path.name for path in self.path.parent.glob("*.lock")), files)
        creates = sum(name == "create" for name, _ in self.engine.events)
        with self.assertRaises(Unavailable):
            self.run_job(complete)
        self.assertEqual(sum(name == "create" for name, _ in self.engine.events), creates)

    def test_retention_skips_a_completed_job_with_lock_still_held(self):
        request = command()
        self.run_job(request)
        with self.registry.job_lock(request.execution_id):
            self.assertEqual(self.registry.redact_completed(1000000), 0)
        self.assertIsNotNone(self.registry.get(request.execution_id)["result"])

    def test_retention_does_not_weaken_nonce_replay_or_job_tombstones(self):
        request = command()
        envelope = signed(request)
        self.service.handle(*envelope, trusted_peer=True)
        self.registry.redact_completed(1000000)
        with self.assertRaises(Denied):
            self.service.handle(*envelope, trusted_peer=True)
        with patch("execution_worker.registry.MAX_JOBS", 1), self.assertRaises(Busy):
            self.run_job()
        self.assertEqual(self.registry.capacity_report()["jobs"], 1)

    def test_capacity_guard_does_not_prevent_known_resource_recovery(self):
        self.engine.fail_remove = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.engine.fail_remove = False
        with patch("execution_worker.registry.MAX_STATE_BYTES", 1):
            self.assertIn(str(request.execution_id), self.service.startup_check(recover_owned=True)["recovered"])
        self.assertEqual(self.state(request), "COMPLETED")
        self.assertFalse(self.engine.rows)
