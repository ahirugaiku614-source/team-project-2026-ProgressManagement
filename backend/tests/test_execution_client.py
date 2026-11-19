"""単一processの4受付枠とworker契約を検証する。実Docker・Python実行は行わない。"""

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from app.editor_contracts import EditorBinding
from app.execution_client import ExecutionBusy, ExecutionClient, ExecutionCommand, ExecutionPolicy, ExecutionReply, WorkerUnavailable
from app.schemas import ExecutionResponse


def command():
    """同一本人でも独立した実行IDを持つ合成要求を作る。"""
    return ExecutionCommand(execution_id=uuid4(), owner=EditorBinding(team_id=1, project_id=2, user_id=3), code="raise AssertionError('never-run')")


class BlockingWorker:
    """4件が到着するまで待つだけのモック。学生コードや外部プロセスを起動しない。"""

    def __init__(self):
        """実時間sleepを避けて競合させる同期道具と、検査用countを作る。"""
        self.lock, self.full, self.release = Lock(), Event(), Event()
        self.active, self.peak, self.calls = 0, 0, 0

    def execute(self, request):
        """同時到着数を記録して解放を待ち、固定の正常応答を返す。"""
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
            if self.active == 4:
                self.full.set()
        try:
            if not self.release.wait(10):
                raise RuntimeError("test synchronization timeout")
            return ExecutionReply(execution_id=request.execution_id, owner=request.owner,
                                  result=ExecutionResponse(status="completed", stdout="", stderr="", exit_code=0, execution_time_ms=1))
        finally:
            with self.lock:
                self.active -= 1


class ExecutionClientTests(unittest.TestCase):
    """4枠・例外時のAPI枠解放・結果束縛・既定未接続を検査する。分散予約は検査しない。"""

    def test_four_inflight_calls_reject_fifth_and_release_after_completion(self):
        worker = BlockingWorker()
        client = ExecutionClient(worker)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(client.execute, command()) for _ in range(4)]
            try:
                self.assertTrue(worker.full.wait(5))
                with self.assertRaises(ExecutionBusy):
                    client.execute(command())
                self.assertEqual(worker.calls, 4)
                self.assertEqual(worker.peak, 4)
            finally:
                worker.release.set()
            self.assertTrue(all(f.result(5).status == "completed" for f in futures))
        self.assertEqual(client.execute(command()).status, "completed")

    def test_worker_failure_returns_api_slots_without_hidden_retry(self):
        worker = BlockingWorker()
        client = ExecutionClient(worker)
        with patch.object(worker, "execute", side_effect=RuntimeError("SYNTHETIC_SECRET")) as called:
            for _ in range(6):
                with self.assertRaises(WorkerUnavailable) as error:
                    client.execute(command())
                self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
            self.assertEqual(called.call_count, 6)

    def test_default_is_disconnected_even_with_environment_flags(self):
        with patch.dict("os.environ", {"EXECUTION_ENABLED": "true", "DOCKER_HOST": "unix:///private.sock"}):
            with self.assertRaises(WorkerUnavailable):
                ExecutionClient().execute(command())

    def test_wrong_execution_id_and_untyped_result_rejected(self):
        worker = BlockingWorker()
        request = command()
        reply = ExecutionReply(execution_id=uuid4(), owner=request.owner,
                               result=ExecutionResponse(status="completed", stdout="", stderr="", exit_code=0, execution_time_ms=1))
        for value in (reply, {"status": "completed"}, None):
            with patch.object(worker, "execute", return_value=value), self.assertRaises(WorkerUnavailable):
                ExecutionClient(worker).execute(request)

    def test_bypassed_invalid_response_is_revalidated(self):
        request, worker = command(), BlockingWorker()
        invalid = ExecutionResponse.model_construct(status="completed", stdout="", stderr="", exit_code=9, execution_time_ms=-1)
        reply = ExecutionReply.model_construct(execution_id=request.execution_id, owner=request.owner, result=invalid)
        with patch.object(worker, "execute", return_value=reply), self.assertRaises(WorkerUnavailable):
            ExecutionClient(worker).execute(request)

    def test_output_bytes_and_result_consistency_are_bounded(self):
        base = dict(status="completed", stdout="", stderr="", exit_code=0, execution_time_ms=1)
        for changes in ({"stdout": "あ" * 22000}, {"stderr": "x" * 65537}, {"stdout": "\ud800"},
                        {"execution_time_ms": True}, {"exit_code": True}, {"execution_time_ms": -1},
                        {"status": "timeout"}, {"status": "error"}, {"exit_code": None}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValidationError):
                ExecutionResponse(**{**base, **changes})
        self.assertIsNone(ExecutionResponse(**{**base, "status": "error", "exit_code": None}).exit_code)

    def test_arbitrary_worker_operations_and_resources_are_rejected(self):
        values = command().model_dump()
        for changes in ({"operation": "DELETE"}, {"image": "arbitrary"}, {"url": "http://private"}, {"code": "\x00"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ExecutionCommand(**{**values, **changes})
        for changes in ({"cpu_millis": 1000}, {"memory_bytes": 256}, {"runtime": "runc"}, {"network": "host"}, {"timeout_ms": 6000}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ExecutionPolicy(**changes)

    def test_api_and_client_do_not_import_execution_or_network_implementations(self):
        root = Path(__file__).resolve().parents[1] / "app"
        forbidden = {"subprocess", "docker", "socket", "requests", "httpx", "urllib", "libvirt"}
        for name in ("execution_api.py", "execution_client.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse(any(alias.name.split(".")[0] in forbidden for alias in node.names))
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module.split(".")[0], forbidden)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"exec", "eval", "compile"})
