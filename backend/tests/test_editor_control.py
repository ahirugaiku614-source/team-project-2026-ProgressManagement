"""個人エディタ制御の認証・所有関係・常時無効の境界を、専用試験DBだけで確認する。"""

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

import test_projects as fixtures
from app import models
from app.editor_contracts import (
    DisabledEditorWorker, EditorBinding, EditorRuntimeUnavailable,
    WorkerCommand, WorkerState, require_editor_runtime, validate_worker_reply,
)


class EditorContractTests(unittest.TestCase):
    def test_binding_key_separates_user_and_project_but_is_not_authentication(self):
        binding = EditorBinding(team_id=1, project_id=1, user_id=1)
        self.assertEqual(binding.environment_key, EditorBinding(team_id=1, project_id=1, user_id=1).environment_key)
        self.assertNotEqual(binding.environment_key, EditorBinding(team_id=1, project_id=1, user_id=2).environment_key)
        self.assertNotEqual(binding.environment_key, EditorBinding(team_id=1, project_id=2, user_id=1).environment_key)

    def test_binding_requires_positive_strict_ids_and_forbids_paths(self):
        for changes in ({"user_id": True}, {"user_id": "1"}, {"project_id": 0}, {"team_id": -1}, {"path": "/host"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                EditorBinding(**{**dict(team_id=1, project_id=1, user_id=1), **changes})

    def test_worker_contract_rejects_unknown_operations_and_runtime_options(self):
        values = dict(operation_id=uuid4(), operation="ENSURE_STARTED", binding=EditorBinding(team_id=1, project_id=1, user_id=1))
        for changes in ({"operation": "EXEC"}, {"operation": "DELETE_DATA"}, {"image": "arbitrary"},
                        {"mounts": ["/var/run/docker.sock"]}, {"url": "https://arbitrary"}, {"password": "private"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                WorkerCommand(**{**values, **changes})

    def test_worker_replies_must_match_user_project_team_and_operation(self):
        command = WorkerCommand(operation_id=uuid4(), operation="GET_STATE", binding=EditorBinding(team_id=1, project_id=1, user_id=1))
        valid = WorkerState(operation_id=command.operation_id, binding=command.binding, state="STOPPED")
        self.assertEqual(validate_worker_reply(command, valid), valid)
        for binding in (EditorBinding(team_id=1, project_id=1, user_id=2), EditorBinding(team_id=1, project_id=2, user_id=1),
                        EditorBinding(team_id=2, project_id=1, user_id=1)):
            with self.assertRaises(ValueError):
                validate_worker_reply(command, WorkerState(operation_id=command.operation_id, binding=binding, state="RUNNING"))
        with self.assertRaises(ValueError):
            validate_worker_reply(command, WorkerState(operation_id=uuid4(), binding=command.binding, state="STOPPED"))

    def test_disabled_worker_and_gate_cannot_be_enabled_with_environment_variables(self):
        command = WorkerCommand(operation_id=uuid4(), operation="ENSURE_STARTED", binding=EditorBinding(team_id=1, project_id=1, user_id=1))
        with patch.dict(os.environ, {"EDITOR_RUNTIME_ENABLED": "true", "EDITOR_WORKER_URL": "http://untrusted"}):
            with self.assertRaises(EditorRuntimeUnavailable):
                DisabledEditorWorker().execute(command)
            with self.assertRaises(EditorRuntimeUnavailable):
                require_editor_runtime()


class EditorControlTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ProjectAPITests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectAPITests.tearDownClass.__func__)
    tearDown = fixtures.ProjectAPITests.tearDown
    request = fixtures.ProjectAPITests.request
    headers = staticmethod(fixtures.ProjectAPITests.headers)
    decode_token = staticmethod(fixtures.ProjectAPITests.decode_token)

    def setUp(self):
        fixtures.ProjectAPITests.setUp(self)
        self.project_id = self.request("POST", json={"name": "Editor project"}).json()["id"]
        self.editor = f"{self.url}/{self.project_id}/editor"
        # 外部Firebase通信だけを代替する。HTTP認証とUID/DB/所属の解決は実装を通す。
        verifier = patch("app.editor_access.auth.verify_id_token", side_effect=lambda token, **kw: self.decode_token(token))
        self.strict_auth = verifier.start()
        self.addCleanup(verifier.stop)

    def test_status_uses_authenticated_user_and_never_reports_running(self):
        for uid, expected_id in (("owner", self.owner.id), ("member", self.member.id)):
            response = self.request("GET", self.editor + "?user_id=999", uid=uid)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["user_id"], expected_id)
            self.assertEqual(response.json()["project_id"], self.project_id)
            self.assertFalse(response.json()["launch_enabled"])
            self.assertEqual(response.json()["state"], "UNAVAILABLE")
            self.assertNotIn("url", response.json())
        self.strict_auth.assert_called_with("member", check_revoked=True)

    def test_all_actions_fail_closed_without_worker_network_or_storage_operations(self):
        from app.project_storage import ProjectStorage
        with patch.object(DisabledEditorWorker, "execute", side_effect=AssertionError("No worker calls")), \
             patch.object(ProjectStorage, "initialize", side_effect=AssertionError("No storage initialization")), \
             patch.dict(os.environ, {"EDITOR_RUNTIME_ENABLED": "true"}):
            for action in ("start", "stop", "sessions"):
                response = self.request("POST", self.editor + "/" + action, json={})
                self.assertEqual(response.status_code, 503, response.text)
                self.assertNotIn("set-cookie", response.headers)
                self.assertNotIn("token", response.text.lower())
                self.assertNotIn("url", response.text.lower())

    def test_unrecognized_body_fields_are_rejected_without_echoing_values(self):
        for action in ("start", "stop", "sessions"):
            for values in ({"user_id": self.owner.id}, {"project_id": 999}, {"url": "private-url"},
                           {"token": "private-token"}, {"password": "private-password"}, {"mount": "/server/private"}):
                response = self.request("POST", self.editor + "/" + action, json=values)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("private", response.text)

    def test_unauthenticated_cookie_query_and_invalid_token_cannot_access(self):
        for method, suffix in (("GET", ""), ("POST", "/start"), ("POST", "/stop"), ("POST", "/sessions")):
            url = self.editor + suffix
            self.assertIn(self.client.request(method, url + "?token=owner", cookies={"session": "owner"}).status_code, (401, 403))
            self.assertEqual(self.request(method, url, uid="invalid").status_code, 401)

    def test_other_team_project_and_unknown_user_are_rejected(self):
        foreign = models.Project(name="Foreign", team_id=self.other_team.id, created_by=self.outsider.id)
        self.db.add(foreign)
        self.db.commit()
        for method, suffix in (("GET", ""), ("POST", "/start"), ("POST", "/stop"), ("POST", "/sessions")):
            self.assertEqual(self.request(method, self.editor + suffix, uid="outsider").status_code, 403)
            self.assertEqual(self.request(method, f"{self.url}/{foreign.id}/editor{suffix}").status_code, 404)
            self.assertEqual(self.request(method, f"{self.url}/99999/editor{suffix}").status_code, 404)
            self.assertEqual(self.request(method, self.editor + suffix, uid="unknown-user").status_code, 404)

    def test_removed_member_loses_all_http_access_on_next_request(self):
        self.assertEqual(self.request("GET", self.editor).status_code, 200)
        self.db.query(models.TeamMember).filter_by(team_id=self.team.id, user_id=self.member.id).delete()
        self.db.commit()
        for method, suffix in (("GET", ""), ("POST", "/start"), ("POST", "/stop"), ("POST", "/sessions")):
            self.assertEqual(self.request(method, self.editor + suffix).status_code, 403)

    def test_revoked_disabled_or_unverifiable_firebase_token_fails_without_details(self):
        for error in (ValueError("private-token /firebase/private-path"), OSError("private-network-error")):
            with patch("app.editor_access.auth.verify_id_token", side_effect=error) as verifier:
                response = self.request("GET", self.editor)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("private", response.text)
                verifier.assert_called_once_with("member", check_revoked=True)
        for claims in ({}, {"uid": None}, {"uid": ""}):
            with patch("app.editor_access.auth.verify_id_token", return_value=claims):
                self.assertEqual(self.request("GET", self.editor).status_code, 401)

    def test_websocket_is_never_accepted_even_with_forged_credentials(self):
        for suffix in ("", "/ws", "/proxy/8080", "/users/999"):
            with self.assertRaises(WebSocketDisconnect) as error:
                with self.client.websocket_connect(self.editor + suffix, headers=self.headers("owner")):
                    self.fail("Editor WebSocket must not be accepted")
            self.assertEqual(error.exception.code, 1008)

    def test_disabled_actions_do_not_add_or_modify_database_records(self):
        before = {table.name: [tuple(row) for row in self.db.execute(table.select()).all()]
                  for table in models.Base.metadata.sorted_tables}
        for action in ("start", "stop", "sessions"):
            self.assertEqual(self.request("POST", self.editor + "/" + action).status_code, 503)
        after = {table.name: [tuple(row) for row in self.db.execute(table.select()).all()]
                 for table in models.Base.metadata.sorted_tables}
        self.assertEqual(before, after)
