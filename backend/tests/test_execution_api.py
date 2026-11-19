"""POST /executeの認証・認可・入力・モック応答を、専用SQLiteだけで確認する。"""

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import test_projects as fixtures
from app import models
from app.execution_client import ExecutionClient, ExecutionReply, get_execution_client
from app.schemas import EXECUTION_CODE_MAX_BYTES, ExecutionResponse


@dataclass
class ResultWorker:
    """コードを評価せず、設定した固定結果を返して送信契約だけを記録するモック。"""

    result: ExecutionResponse
    commands: list = field(default_factory=list)

    def execute(self, command):
        """入力を実行せず、実行IDと所有者に対応した結果を組み立てる。"""
        self.commands.append(command)
        return ExecutionReply(execution_id=command.execution_id, owner=command.owner, result=self.result)


class ExecutionAPITests(unittest.TestCase):
    """外部Firebase検証だけをmockし、Bearer・UID/DB・所属の実処理を通す。"""

    setUpClass = classmethod(fixtures.ProjectAPITests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectAPITests.tearDownClass.__func__)
    tearDown = fixtures.ProjectAPITests.tearDown
    request = fixtures.ProjectAPITests.request
    headers = staticmethod(fixtures.ProjectAPITests.headers)
    decode_token = staticmethod(fixtures.ProjectAPITests.decode_token)

    def setUp(self):
        """既存fixturesの独立DBに合成Projectを作り、失効確認付き検証器を代替する。"""
        fixtures.ProjectAPITests.setUp(self)
        self.project_id = self.request("POST", json={"name": "Execution project"}).json()["id"]
        verifier = patch("app.editor_access.auth.verify_id_token", side_effect=lambda token, **kw: self.decode_token(token))
        self.verifier = verifier.start()
        self.addCleanup(verifier.stop)
        self.worker = ResultWorker(ExecutionResponse(status="completed", stdout="Hello\n", stderr="", exit_code=0, execution_time_ms=12))

    def payload(self, **changes):
        """所有者IDを含まない正規本文に、検証対象の変更だけを加える。"""
        return {"team_id": self.team.id, "project_id": self.project_id, "language": "python", "code": 'print("Hello")', **changes}

    def connected(self):
        """このテストだけでmockを依存注入する。実workerを有効化する設定ではない。"""
        client = ExecutionClient(self.worker)
        self.main.app.dependency_overrides[get_execution_client] = lambda: client
        return client

    def post(self, uid="member", **changes):
        """/executeへBearer認証付きで要求する。"""
        return self.request("POST", "/execute", uid=uid, json=self.payload(**changes))

    def test_default_disconnected_worker_returns_503(self):
        response = self.post()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("print", response.text)

    def test_completed_result_and_authenticated_owner_are_forwarded(self):
        self.connected()
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), self.worker.result.model_dump())
        command = self.worker.commands[0]
        self.assertEqual(command.owner.user_id, self.member.id)
        self.assertEqual(command.owner.team_id, self.team.id)
        self.assertEqual(command.owner.project_id, self.project_id)
        self.assertEqual(command.policy.cpu_millis, 500)
        self.assertEqual(command.policy.memory_bytes, 128 * 1024**2)
        self.assertEqual(command.policy.timeout_ms, 5000)
        self.assertEqual(command.policy.runtime, "runsc")
        self.assertEqual(command.policy.network, "none")
        self.verifier.assert_called_with("member", check_revoked=True)

    def test_owner_and_member_may_execute_as_themselves(self):
        self.connected()
        for uid, user_id in (("owner", self.owner.id), ("member", self.member.id)):
            self.assertEqual(self.post(uid=uid).status_code, 200)
            self.assertEqual(self.worker.commands[-1].owner.user_id, user_id)

    def test_execution_error_has_nonzero_code(self):
        self.worker.result = ExecutionResponse(status="error", stdout="", stderr="SyntaxError: synthetic\n", exit_code=1, execution_time_ms=3)
        self.connected()
        response = self.post(code="invalid python syntax")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.worker.result.model_dump())

    def test_timeout_has_null_exit_code(self):
        self.worker.result = ExecutionResponse(status="timeout", stdout="partial", stderr="", exit_code=None, execution_time_ms=5000)
        self.connected()
        response = self.post(code="while True: pass")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["exit_code"])
        self.assertEqual(response.json()["status"], "timeout")

    def test_no_auth_cookie_or_query_token_is_accepted(self):
        self.connected()
        response = self.client.post("/execute?token=member", cookies={"token": "member"}, json=self.payload())
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(self.worker.commands)

    def test_invalid_and_revoked_token_and_auth_outage_fail_closed(self):
        self.connected()
        self.assertEqual(self.post(uid="invalid").status_code, 401)
        for exception in (ValueError("SYNTHETIC_TOKEN"), ConnectionError("SYNTHETIC_TOKEN")):
            self.verifier.side_effect = exception
            response = self.post()
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("SYNTHETIC_TOKEN", response.text)
        self.assertFalse(self.worker.commands)

    def test_nonmember_and_unknown_user_cannot_execute(self):
        self.connected()
        self.assertEqual(self.post(uid="outsider").status_code, 403)
        self.assertEqual(self.post(uid="unknown-user").status_code, 404)
        self.assertFalse(self.worker.commands)

    def test_other_project_and_team_pair_and_missing_project_rejected(self):
        self.connected()
        foreign = models.Project(name="Foreign", team_id=self.other_team.id, created_by=self.outsider.id)
        self.db.add(foreign)
        self.db.commit()
        self.assertEqual(self.post(project_id=foreign.id).status_code, 404)
        self.assertEqual(self.post(team_id=self.other_team.id, project_id=foreign.id).status_code, 403)
        self.assertEqual(self.post(project_id=999999).status_code, 404)
        self.assertFalse(self.worker.commands)

    def test_membership_removal_is_observed_on_next_request(self):
        self.connected()
        self.assertEqual(self.post().status_code, 200)
        self.db.query(models.TeamMember).filter_by(team_id=self.team.id, user_id=self.member.id).delete()
        self.db.commit()
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(len(self.worker.commands), 1)

    def test_empty_invalid_type_nul_and_blank_source_rejected(self):
        self.connected()
        for code in ("", " \r\n\t", "bad\x00code", None, 123, ["print(1)"]):
            with self.subTest(code=code):
                self.assertEqual(self.post(code=code).status_code, 422)
        self.assertFalse(self.worker.commands)

    def test_exact_byte_limit_accepted_and_multibyte_overflow_rejected(self):
        self.connected()
        self.assertEqual(self.post(code="#" + "a" * (EXECUTION_CODE_MAX_BYTES - 1)).status_code, 200)
        self.assertEqual(self.post(code="a" * (EXECUTION_CODE_MAX_BYTES + 1)).status_code, 422)
        self.assertEqual(self.post(code="あ" * (EXECUTION_CODE_MAX_BYTES // 3 + 1)).status_code, 422)
        self.assertEqual(len(self.worker.commands), 1)

    def test_indentation_and_code_text_are_not_rewritten_or_executed(self):
        self.connected()
        code = "if True:\n    raise AssertionError('must-not-run')\n"
        self.assertEqual(self.post(code=code).status_code, 200)
        self.assertEqual(self.worker.commands[0].code, code)

    def test_language_ids_and_extra_owner_or_runtime_fields_rejected(self):
        self.connected()
        for changes in ({"language": "javascript"}, {"language": None}, {"project_id": "1"}, {"team_id": True},
                        {"project_id": 0}, {"project_id": 2**80}, {"user_id": self.owner.id}, {"student_id": self.owner.id},
                        {"image": "private"}, {"runtime": "runc"}, {"mounts": ["/host"]}):
            with self.subTest(changes=changes):
                self.assertEqual(self.post(**changes).status_code, 422)
        self.assertFalse(self.worker.commands)

    def test_validation_errors_do_not_reflect_source_or_unknown_fields(self):
        for payload in (self.payload(code="SYNTHETIC_SECRET\x00"), self.payload(token="SYNTHETIC_SECRET"), self.payload(code="SYNTHETIC_SECRET", language="bash")):
            response = self.request("POST", "/execute", json=payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("SYNTHETIC_SECRET", response.text)

    def test_invalid_json_and_invalid_unicode_are_sanitized(self):
        for data in (b'{"code":"SYNTHETIC_SECRET",', b'{"code":"\xff"}',
                     b'{"team_id":1,"project_id":1,"code":"\\ud800"}'):
            response = self.client.post("/execute", headers={**self.headers(), "Content-Type": "application/json"}, content=data)
            self.assertIn(response.status_code, (400, 422), response.text)
            self.assertNotIn("SYNTHETIC_SECRET", response.text)

    def test_worker_exception_is_fixed_503(self):
        self.connected()
        with patch.object(self.worker, "execute", side_effect=RuntimeError("SYNTHETIC_SECRET /server/private")):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SYNTHETIC_SECRET", response.text)
        self.assertNotIn("/server/private", response.text)

    def test_mismatched_worker_owner_is_not_leaked(self):
        self.connected()
        def wrong_owner(command):
            """他者の結果を返す故障を合成する。"""
            return ExecutionReply(execution_id=command.execution_id,
                                  owner=command.owner.model_copy(update={"user_id": self.owner.id}),
                                  result=ExecutionResponse(status="completed", stdout="SYNTHETIC_SECRET", stderr="", exit_code=0, execution_time_ms=1))
        with patch.object(self.worker, "execute", side_effect=wrong_owner):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SYNTHETIC_SECRET", response.text)

    def test_missing_required_fields_are_rejected(self):
        for key in ("team_id", "project_id", "code"):
            values = self.payload()
            del values[key]
            self.assertEqual(self.request("POST", "/execute", json=values).status_code, 422)

    def test_fifth_request_gets_429_while_four_mock_calls_are_inflight(self):
        from test_execution_client import BlockingWorker, command
        worker = BlockingWorker()
        client = ExecutionClient(worker)
        self.main.app.dependency_overrides[get_execution_client] = lambda: client
        # DBを複数threadで共有しない。4枠は直接clientのmockで占有し、HTTPは1件だけ通す。
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(client.execute, command()) for _ in range(4)]
            try:
                self.assertTrue(worker.full.wait(5))
                response = self.post()
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.headers["Retry-After"], "1")
                self.assertEqual(worker.calls, 4)
            finally:
                worker.release.set()
            for future in futures:
                future.result(5)
