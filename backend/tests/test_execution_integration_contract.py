"""PR #19の設計前提を既存APIで検査する。UI接続・実通信・学生コードの実行はしない。"""

import unittest

import test_execution_api as base
from app import models
from app.schemas import EXECUTION_CODE_MAX_BYTES


class ExecutionIntegrationContractTests(unittest.TestCase):
    """既存の一時DB・合成Firebase・結果固定workerを再利用し、認可を重複実装しない。"""

    setUpClass = classmethod(base.ExecutionAPITests.setUpClass.__func__)
    tearDownClass = classmethod(base.ExecutionAPITests.tearDownClass.__func__)
    setUp = base.ExecutionAPITests.setUp
    tearDown = base.ExecutionAPITests.tearDown
    request = base.ExecutionAPITests.request
    headers = staticmethod(base.ExecutionAPITests.headers)
    decode_token = staticmethod(base.ExecutionAPITests.decode_token)
    payload = base.ExecutionAPITests.payload
    connected = base.ExecutionAPITests.connected
    post = base.ExecutionAPITests.post

    def test_unsaved_text_snapshot_keeps_whitespace_and_owner_binding(self):
        """未保存バッファ相当の本文を固定し、保存APIやファイル名なしで既存契約へ渡す。"""
        self.connected()
        text = "# 未保存の合成データ\r\nif True:\r\n    raise AssertionError('hostで実行禁止')\r\n"
        response = self.post(code=text)
        self.assertEqual(response.status_code, 200)
        command = self.worker.commands[0]
        self.assertEqual(command.code, text)
        self.assertEqual(command.owner.user_id, self.member.id)
        self.assertEqual(command.owner.project_id, self.project_id)
        self.assertEqual(command.operation, "RUN_PYTHON")

    def test_future_languages_are_rejected_by_the_unchanged_legacy_api(self):
        """将来候補を列挙しても、現APIのPython許可リストを広げない。"""
        self.connected()
        for language in ("c", "cpp", "java", "javascript", "typescript", "csharp", "go", "rust",
                         "unknown", "C++", "node", "py"):
            with self.subTest(language=language):
                response = self.post(language=language, code="SYNTHETIC_SOURCE")
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("SYNTHETIC_SOURCE", response.text)
        self.assertFalse(self.worker.commands)

    def test_future_operation_and_free_compiler_settings_are_rejected(self):
        """RUN_CODE案や任意image・コンパイラ設定を現APIへ混ぜても要求を通さない。"""
        self.connected()
        for extra in ({"operation": "RUN_CODE"}, {"version": 2}, {"image": "SYNTHETIC_SECRET"},
                      {"command": ["sh", "-c", "SYNTHETIC_SECRET"]},
                      {"compiler_options": ["SYNTHETIC_SECRET"]}, {"profile_id": "SYNTHETIC_SECRET"}):
            with self.subTest(field=next(iter(extra))):
                response = self.post(code="SYNTHETIC_SOURCE", **extra)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("SYNTHETIC_SOURCE", response.text)
                self.assertNotIn("SYNTHETIC_SECRET", response.text)
        self.assertFalse(self.worker.commands)

    def test_paths_tokens_and_bridge_metadata_are_not_execution_inputs(self):
        """パスや認証値、画面間nonceを本文へ混入させず、エラーにも反射させない。"""
        self.connected()
        for name in ("path", "filename", "workspace", "token", "authorization", "user_id",
                     "request_id", "generation", "document_version", "origin"):
            with self.subTest(field=name):
                response = self.post(code="SYNTHETIC_SOURCE", **{name: "SYNTHETIC_SECRET"})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("SYNTHETIC_SECRET", response.text)
                self.assertNotIn("SYNTHETIC_SOURCE", response.text)
        self.assertFalse(self.worker.commands)

    def test_unauthenticated_snapshot_never_reaches_worker(self):
        """code-server相当の送信元でも、CookieやURLのtokenだけで認証を代替できない。"""
        self.connected()
        response = self.client.post("/execute?token=SYNTHETIC_TOKEN",
                                    cookies={"token": "SYNTHETIC_TOKEN"},
                                    json=self.payload(code="SYNTHETIC_SOURCE"))
        self.assertIn(response.status_code, (401, 403))
        self.assertNotIn("SYNTHETIC_SOURCE", response.text)
        self.assertNotIn("SYNTHETIC_TOKEN", response.text)
        self.assertFalse(self.worker.commands)

    def test_foreign_project_snapshot_never_reaches_worker(self):
        """他ProjectのIDを付けた本文は、ログイン済みでも所属確認で止まる。"""
        self.connected()
        foreign = models.Project(name="合成の別Project", team_id=self.other_team.id, created_by=self.outsider.id)
        self.db.add(foreign)
        self.db.commit()
        for team_id in (self.team.id, self.other_team.id):
            response = self.post(team_id=team_id, project_id=foreign.id, code="SYNTHETIC_SOURCE")
            self.assertIn(response.status_code, (403, 404))
            self.assertNotIn("SYNTHETIC_SOURCE", response.text)
        self.assertFalse(self.worker.commands)

    def test_membership_revocation_after_draft_capture_is_rechecked(self):
        """バッファ取得後に脱退した状況でも、送信時の現在所属を確認する。"""
        self.connected()
        captured = self.payload(code="SYNTHETIC_SOURCE")
        self.db.query(models.TeamMember).filter_by(team_id=self.team.id, user_id=self.member.id).delete()
        self.db.commit()
        response = self.request("POST", "/execute", json=captured)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("SYNTHETIC_SOURCE", response.text)
        self.assertFalse(self.worker.commands)

    def test_utf8_snapshot_limit_is_shared_with_the_existing_api(self):
        """64KiBちょうどは通し、複数byte文字で超過した本文はworkerより前で拒否する。"""
        self.connected()
        boundary = "#" + "あ" * ((EXECUTION_CODE_MAX_BYTES - 1) // 3)
        self.assertEqual(len(boundary.encode("utf-8")), EXECUTION_CODE_MAX_BYTES)
        self.assertEqual(self.post(code=boundary).status_code, 200)
        self.assertEqual(self.post(code=boundary + "あ").status_code, 422)
        self.assertEqual(len(self.worker.commands), 1)

    def test_default_disconnected_path_does_not_enable_a_snapshot(self):
        """UIや言語設計を追加しても、未接続の既定経路は503と同じ結果契約を保つ。"""
        response = self.post(code="SYNTHETIC_SOURCE")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SYNTHETIC_SOURCE", response.text)
        self.assertFalse(self.worker.commands)
