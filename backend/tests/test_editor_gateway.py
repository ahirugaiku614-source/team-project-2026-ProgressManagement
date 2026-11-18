"""HTTPS/Firebase/VMへ接続せず、認可と接続期限の契約をモック・一時DBで検証する。"""

from dataclasses import replace
import io
import multiprocessing
from pathlib import Path
import secrets
import tempfile
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from app.editor_contracts import EditorBinding, EditorRuntimeUnavailable, require_editor_runtime
from editor_gateway.connection import ConnectionGuard
from editor_gateway.contracts import AccessDenied
from editor_gateway.gateway import Gateway, challenge_for, cookie_header, editor_host, session_cookie, upstream_headers
from editor_gateway.store import TestSessionStore
from editor_gateway_fakes import Authority, Clock, Directory, concurrent_exchange, editor_context, exchange_context, issue_context


class GatewayFixture:
    """複数の試験クラスへ一時ストアとモックだけを提供する。テストメソッドは持たない。"""

    def setUp(self):
        """毎回新しい私有一時DBを作り、既存DB/VM管理レジストリを試験へ渡さない。"""
        self.temp = tempfile.TemporaryDirectory(prefix="editor-gateway-test-")
        self.addCleanup(self.temp.cleanup)
        self.store = TestSessionStore.create_new(Path(self.temp.name) / "sessions.sqlite")
        self.clock, self.auth = Clock(), Authority()
        self.directory = Directory(self.clock, self.auth.binding)
        self.gateway = Gateway(self.store, self.auth, self.directory, self.clock)
        self.verifier = secrets.token_urlsafe(32)

    def code(self):
        """アプリ上の本人検証を経て接続コードを発行する試験ヘルパー。"""
        return self.gateway.issue("synthetic-id-token", 1, challenge_for(self.verifier), issue_context())

    def session(self):
        """発行コードを正しいverifierで消費し、新規セッションへ交換する。"""
        code = self.code()
        return self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))


class EditorGatewayTests(GatewayFixture, unittest.TestCase):
    """認証・所属・所有者・一回性・CSRF・接続期限を、実通信なしで横断的に確認する。"""

    def test_authenticated_owner_and_revocation_contract(self):
        grant = self.session()
        access = self.gateway.authorize(grant.session, editor_context(grant.host))
        self.assertEqual(access.binding, self.auth.binding)
        self.assertEqual(access.generation, 1)
        self.assertEqual(access.deadline_ms, self.clock() + 15000)
        self.assertTrue(all(flag is True for _, flag in self.auth.checks))

    def test_s256_matches_rfc7636_known_vector(self):
        self.assertEqual(challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_all_reference_operations_use_no_network_or_command(self):
        with patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS")), \
                patch("subprocess.run", side_effect=AssertionError("CLI")):
            grant = self.session()
            self.gateway.authorize(grant.session, editor_context(grant.host))
            guard = ConnectionGuard(self.gateway, grant.session, editor_context(grant.host))
            guard.reauthorize()
            self.gateway.revoke(grant.session, editor_context(grant.host, "POST"), grant.csrf)

    def test_malformed_identity_and_generation_are_rejected(self):
        identity = self.auth.identity
        for changes in ({"uid": ""}, {"uid": "bad\nuid"}, {"auth_time_ms": self.clock() + 1},
                        {"auth_time_ms": True}, {"token_expires_ms": self.clock()}):
            self.auth.identity = replace(identity, **changes)
            with self.assertRaises(AccessDenied):
                self.code()
        self.auth.identity = identity
        for generation in (True, 0, -1, "1"):
            self.directory.generation = generation
            with self.assertRaises(AccessDenied):
                self.code()

    def test_invalid_token_and_unknown_project(self):
        for token, project in (("invalid", 1), ("synthetic-id-token", 2), ("synthetic-id-token", True)):
            with self.assertRaises(AccessDenied):
                self.gateway.issue(token, project, challenge_for(self.verifier), issue_context())

    def test_other_vm_owner_denied(self):
        self.directory.binding = EditorBinding(team_id=1, project_id=1, user_id=2)
        with self.assertRaises(AccessDenied):
            self.code()

    def test_other_disk_owner_denied(self):
        self.directory.disk_owner = EditorBinding(team_id=1, project_id=1, user_id=2)
        with self.assertRaises(AccessDenied):
            self.code()

    def test_other_editor_host_cannot_reuse_cookie(self):
        grant = self.session()
        other = editor_host(EditorBinding(team_id=1, project_id=2, user_id=1))
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(grant.session, editor_context(other))

    def test_crosswired_stored_host_is_not_trusted(self):
        grant, code = self.session(), self.code()
        other = editor_host(EditorBinding(team_id=1, project_id=2, user_id=1))
        # 一時DBだけに破損を注入し、保存hostを所有関係より優先しないことを確認する。
        with self.store.connection() as db:
            db.execute("UPDATE sessions SET host=?", (other,))
            db.execute("UPDATE codes SET host=?", (other,))
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(grant.session, editor_context(other))
        with self.assertRaises(AccessDenied):
            self.gateway.exchange(code.code, self.verifier, exchange_context(other))

    def test_bearer_cookie_replay_on_same_host_is_a_documented_residual_risk(self):
        grant = self.session()
        # 同一ホストの有効Cookie再送は通常の複数接続と区別できない。完全防止を主張しない。
        for _ in range(2):
            self.gateway.authorize(grant.session, editor_context(grant.host))
        self.gateway.revoke(grant.session, editor_context(grant.host, "POST"), grant.csrf)
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(grant.session, editor_context(grant.host))

    def test_membership_removal_rejects_http_and_websocket(self):
        grant = self.session()
        self.auth.member = False
        for websocket in (False, True):
            with self.assertRaises(AccessDenied):
                self.gateway.authorize(grant.session, editor_context(grant.host), websocket=websocket)

    def test_project_moved_to_different_team_rejects_old_session(self):
        grant = self.session()
        self.auth.binding = EditorBinding(team_id=2, project_id=1, user_id=1)
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(grant.session, editor_context(grant.host))

    def test_firebase_revocation_blocks_issue_exchange_and_existing_session(self):
        grant, code = self.session(), self.code()
        self.auth.revoked = True
        for operation in (self.code, lambda: self.gateway.exchange(code.code, self.verifier, exchange_context(code.host)),
                          lambda: self.gateway.authorize(grant.session, editor_context(grant.host))):
            with self.assertRaises(AccessDenied):
                operation()

    def test_code_reuse_denied(self):
        code = self.code()
        self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))
        with self.assertRaises(AccessDenied):
            self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))

    def test_wrong_verifier_and_plain_challenge_rejected(self):
        code = self.code()
        with self.assertRaises(AccessDenied):
            self.gateway.exchange(code.code, "x" * 43, exchange_context(code.host))
        # 正しいverifierなら交換可能。失敗回数制限は将来のHTTP境界で別途必要。
        self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))
        for value in ("short", "x" * 129, "x" * 42 + "\n"):
            with self.assertRaises(AccessDenied):
                challenge_for(value)

    def test_code_expires_at_thirty_seconds(self):
        code = self.code()
        self.assertEqual(code.expires_ms, self.clock() + 30000)
        self.clock.now += 30000
        with self.assertRaises(AccessDenied):
            self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))

    def test_session_absolute_five_minute_expiry(self):
        grant = self.session()
        self.assertEqual(grant.expires_ms, self.clock() + 300000)
        self.clock.now += 299999
        self.gateway.authorize(grant.session, editor_context(grant.host))
        self.clock.now += 1
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(grant.session, editor_context(grant.host))

    def test_session_cannot_outlive_firebase_token(self):
        self.auth.identity = replace(self.auth.identity, token_expires_ms=self.clock() + 20000)
        grant = self.session()
        self.assertEqual(grant.expires_ms, self.auth.identity.token_expires_ms)
        self.clock.now += 20000
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(grant.session, editor_context(grant.host))

    def test_vm_generation_change_rejects_old_code_and_session(self):
        grant, code = self.session(), self.code()
        self.directory.generation += 1
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(grant.session, editor_context(grant.host))
        with self.assertRaises(AccessDenied):
            self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))

    def test_stopped_quarantined_unknown_and_unready_vm_rejected(self):
        grant = self.session()
        for state in ("STOPPED", "QUARANTINED", "STARTING", "UNKNOWN", "ABSENT"):
            self.directory.state = state
            with self.assertRaises(AccessDenied):
                self.gateway.authorize(grant.session, editor_context(grant.host))
        self.directory.state, self.directory.ready = "RUNNING", False
        with self.assertRaises(AccessDenied):
            self.code()

    def test_stale_or_unbounded_vm_evidence_rejected(self):
        route = self.directory.lookup(self.auth.binding)
        for deadline in (self.clock(), self.clock() + 15001):
            with patch.object(self.directory, "lookup", return_value=replace(route, valid_until_ms=deadline)), self.assertRaises(AccessDenied):
                self.code()

    def test_session_revocation(self):
        grant = self.session()
        self.gateway.revoke(grant.session, editor_context(grant.host, "POST"), grant.csrf)
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(grant.session, editor_context(grant.host))

    def test_cookie_secure_host_only_attributes_and_parsing(self):
        grant = self.session()
        header = cookie_header(grant, self.clock())
        for attribute in ("__Host-editor=", "Path=/", "Max-Age=300", "Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(attribute, header)
        self.assertNotIn("Domain", header)
        self.assertEqual(session_cookie("other=value; __Host-editor=" + grant.session), grant.session)
        for bad in ("__Host-editor=" + grant.session + "; __Host-editor=" + grant.session, "__Host-editor=bad", "__Host-editor=" + grant.session + "\r\n"):
            with self.assertRaises(AccessDenied):
                session_cookie(bad)

    def test_csrf_required_for_unsafe_http(self):
        grant = self.session()
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            context = editor_context(grant.host, method)
            for csrf in (None, "x" * 43):
                with self.assertRaises(AccessDenied):
                    self.gateway.authorize(grant.session, context, csrf=csrf)
            self.gateway.authorize(grant.session, context, csrf=grant.csrf)

    def test_host_sni_origin_scheme_and_fetch_site_are_exact(self):
        grant = self.session()
        context = editor_context(grant.host)
        for changes in ({"host": grant.host + ".evil.invalid"}, {"host": grant.host + ":443"}, {"host": grant.host.upper()},
                        {"tls_server_name": "attacker.invalid"}, {"scheme": "http"}, {"origin": "null"},
                        {"origin": "https://evil.invalid"}, {"fetch_site": "cross-site"}, {"fetch_site": "same-site"}):
            for websocket in (False, True):
                with self.subTest(changes=changes, websocket=websocket), self.assertRaises(AccessDenied):
                    self.gateway.authorize(grant.session, replace(context, **changes), websocket=websocket)

    def test_ws_requires_origin_but_safe_navigation_may_omit_it(self):
        grant = self.session()
        context = replace(editor_context(grant.host), origin=None)
        self.gateway.authorize(grant.session, context)
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(grant.session, context, websocket=True)

    def test_issue_and_exchange_reject_csrf_and_wrong_host(self):
        code = self.code()
        for changes in ({"origin": "https://evil.invalid"}, {"method": "GET"}, {"host": "other.invalid"},
                        {"tls_server_name": "other.invalid"}, {"content_type": "text/plain"}, {"fetch_site": "same-origin"}):
            with self.assertRaises(AccessDenied):
                self.gateway.exchange(code.code, self.verifier, replace(exchange_context(code.host), **changes))
        with self.assertRaises(AccessDenied):
            self.gateway.issue("synthetic-id-token", 1, challenge_for(self.verifier), replace(issue_context(), origin="https://evil.invalid"))

    def test_fixation_prevented_and_csrf_tokens_are_session_specific(self):
        first, second = self.session(), self.session()
        self.assertNotEqual(first.session, second.session)
        self.assertNotEqual(first.csrf, second.csrf)
        with self.assertRaises(AccessDenied):
            self.gateway.authorize(second.session, editor_context(second.host, "POST"), csrf=first.csrf)

    def test_authority_and_directory_outage_fail_closed(self):
        grant = self.session()
        for dependency in (self.auth, self.directory):
            dependency.failure = True
            with self.assertRaises(AccessDenied):
                self.gateway.authorize(grant.session, editor_context(grant.host))
            dependency.failure = False

    def test_secret_values_are_not_logged_printed_or_in_exception_repr(self):
        grant, code = self.session(), self.code()
        output = io.StringIO()
        self.auth.failure = True
        with redirect_stdout(output), redirect_stderr(output):
            try:
                self.gateway.authorize(grant.session, editor_context(grant.host))
            except AccessDenied as error:
                formatted = "".join(traceback.format_exception(error)) + repr(error)
        for secret in ("SYNTHETIC_SECRET", "synthetic-id-token", grant.session, grant.csrf, code.code, self.verifier):
            self.assertNotIn(secret, output.getvalue() + formatted + repr(grant) + repr(code) + repr(self.auth.identity))

    def test_store_only_contains_hashes_of_browser_secrets(self):
        code = self.code()
        grant = self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))
        data = self.store.path.read_bytes()
        for secret in (code.code, grant.session, grant.csrf, self.verifier, "synthetic-id-token"):
            self.assertNotIn(secret.encode(), data)

    def test_sensitive_headers_and_vm_cookie_are_not_forwarded(self):
        headers = (("Authorization", "secret"), ("Cookie", "secret"), ("X-Editor-CSRF", "secret"),
                   ("Forwarded", "secret"), ("X-Forwarded-Host", "secret"), ("Host", "secret"), ("Accept", "text/html"))
        self.assertEqual(upstream_headers(headers), (("accept", "text/html"),))
        response = upstream_headers((("Set-Cookie", "__Host-editor=secret"), ("Location", "https://evil.invalid"), ("Content-Type", "text/html")), response=True)
        self.assertNotIn("secret", repr(response))
        self.assertIn(("cache-control", "no-store"), response)
        for headers in ((("Cookie", "a"), ("cookie", "b")), (("Accept", "a\r\nb"),)):
            with self.assertRaises(AccessDenied):
                upstream_headers(headers)

    def test_disabled_defaults_and_existing_gate_no_network(self):
        gateway = Gateway(self.store, clock_ms=self.clock)
        with patch("socket.create_connection", side_effect=AssertionError("network")), patch("subprocess.run", side_effect=AssertionError("CLI")):
            with self.assertRaises(AccessDenied):
                gateway.issue("synthetic-id-token", 1, challenge_for(self.verifier), issue_context())
            with self.assertRaises(EditorRuntimeUnavailable):
                require_editor_runtime()

    def test_restart_preserves_consumed_codes_and_revoked_sessions(self):
        code = self.code()
        grant = self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))
        self.gateway.revoke(grant.session, editor_context(grant.host, "POST"), grant.csrf)
        other = Gateway(TestSessionStore(self.store.path), self.auth, self.directory, self.clock)
        with self.assertRaises(AccessDenied):
            other.exchange(code.code, self.verifier, exchange_context(code.host))
        with self.assertRaises(AccessDenied):
            other.authorize(grant.session, editor_context(grant.host))

    def test_exchange_db_failure_rolls_back_code_consumption(self):
        code = self.code()
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_session BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECRET'); END")
        with self.assertRaises(AccessDenied):
            self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT used FROM codes").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            db.execute("DROP TRIGGER fail_session")
        self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))

    def test_exchange_expiring_during_authorization_creates_no_session(self):
        code = self.code()
        original = self.auth.check_session
        def delayed(*args, **kwargs):
            """コード期限直後に認可応答が返る場合を模擬する。"""
            self.clock.now += 30000
            return original(*args, **kwargs)
        with patch.object(self.auth, "check_session", side_effect=delayed), self.assertRaises(AccessDenied):
            self.gateway.exchange(code.code, self.verifier, exchange_context(code.host))
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

    def test_opaque_session_does_not_accept_token_or_code_as_cookie(self):
        code = self.code()
        for value in (None, "synthetic-id-token", code.code):
            with self.assertRaises(AccessDenied):
                self.gateway.authorize(value, editor_context(code.host))

    def test_clock_rollback_and_unknown_store_refused(self):
        self.session()
        self.clock.now -= 1
        with self.assertRaises(AccessDenied):
            self.code()
        path = Path(self.temp.name) / "unrelated"
        path.write_bytes(b"must-keep")
        with self.assertRaises(FileExistsError):
            TestSessionStore.create_new(path)
        self.assertEqual(path.read_bytes(), b"must-keep")
        with self.assertRaises(AccessDenied):
            TestSessionStore(path)

    def test_multiple_processes_only_one_can_exchange_code(self):
        code = self.code()
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(4), context.Queue()
        children = [context.Process(target=concurrent_exchange, args=(str(self.store.path), code.code, self.verifier, code.host, barrier, queue)) for _ in range(4)]
        try:
            for child in children:
                child.start()
            results = [queue.get(timeout=40) for _ in children]
            for child in children:
                child.join(timeout=20)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(results.count("success"), 1, results)
            self.assertEqual(results.count("editor_access_denied"), 3, results)
            with self.store.connection() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=10)
            queue.close()
            queue.join_thread()


class ConnectionGuardTests(GatewayFixture, unittest.TestCase):
    """HTTPと同じ認可を使い、仮想時刻でWS/長時間HTTPの閉鎖期限を検証する。"""

    def test_ws_member_revocation_rechecked_within_fifteen_seconds(self):
        grant = self.session()
        guard = ConnectionGuard(self.gateway, grant.session, editor_context(grant.host))
        self.auth.member = False
        self.clock.now += 10000
        with self.assertRaises(AccessDenied):
            guard.reauthorize()
        self.assertTrue(guard.closed)

    def test_idle_deadline_closes_without_waiting_for_next_frame(self):
        grant = self.session()
        guard = ConnectionGuard(self.gateway, grant.session, editor_context(grant.host))
        self.clock.now += 15000
        self.assertFalse(guard.tick())  # 仮想watchdogの呼出し。実タイマーはまだ実装しない。
        with self.assertRaises(AccessDenied):
            guard.permit_forward()

    def test_slow_or_failed_reauthorization_never_extends_old_permission(self):
        grant = self.session()
        guard = ConnectionGuard(self.gateway, grant.session, editor_context(grant.host))
        original = self.auth.check_session
        def delayed(*args, **kwargs):
            """返答が成功でも旧転送期限を越えた場合を模擬する。"""
            self.clock.now += 15000
            return original(*args, **kwargs)
        with patch.object(self.auth, "check_session", side_effect=delayed), self.assertRaises(AccessDenied):
            guard.reauthorize()
        self.assertTrue(guard.closed)

    def test_successful_rechecks_do_not_extend_session_absolute_expiry(self):
        grant = self.session()
        guard = ConnectionGuard(self.gateway, grant.session, editor_context(grant.host), websocket=False)
        for _ in range(29):
            self.clock.now += 10000
            self.assertLessEqual(guard.reauthorize().deadline_ms, grant.expires_ms)
        self.clock.now += 10000
        with self.assertRaises(AccessDenied):
            guard.permit_forward()
        self.assertTrue(guard.closed)

    def test_connection_authentication_outage_or_vm_change_closes(self):
        for failure in ("authority", "generation", "revoke"):
            self.auth.failure = False
            grant = self.session()
            guard = ConnectionGuard(self.gateway, grant.session, editor_context(grant.host))
            if failure == "authority":
                self.auth.failure = True
            elif failure == "generation":
                self.directory.generation += 1
            else:
                self.gateway.revoke(grant.session, editor_context(grant.host, "POST"), grant.csrf)
            with self.assertRaises(AccessDenied):
                guard.reauthorize()
            self.assertTrue(guard.closed)

    def test_closed_connection_and_clock_rollback_never_reopen(self):
        grant = self.session()
        guard = ConnectionGuard(self.gateway, grant.session, editor_context(grant.host))
        self.clock.now -= 1
        self.assertFalse(guard.tick())
        self.clock.now += 1
        with self.assertRaises(AccessDenied):
            guard.reauthorize()
        self.assertTrue(guard.closed)


if __name__ == "__main__":
    unittest.main()
