"""判定APIを実Git・隔離DBで検証する。実DB・既存ボリュームは使用しない。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
import subprocess
import sys
import threading
import unittest
import json
import zlib
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import test_projects as fixtures
import test_change_requests as submissions
from app import models
from app.change_request_reviews import decide_change_request
from app.project_storage import StorageConflict, StorageUnavailable
from app.review_git import ReviewGit


@unittest.skipUnless(sys.platform == "linux", "Review decisions require Linux Git validation")
class ChangeRequestReviewAPITests(unittest.TestCase):
    # 既存の認証・チーム・一時保存領域のフィクスチャを使い、実装本体だけを通す。
    setUpClass = classmethod(fixtures.ProjectAPITests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectAPITests.tearDownClass.__func__)
    tearDown = fixtures.ProjectAPITests.tearDown
    request = fixtures.ProjectAPITests.request
    headers = staticmethod(fixtures.ProjectAPITests.headers)
    decode_token = staticmethod(fixtures.ProjectAPITests.decode_token)
    submit = submissions.ChangeRequestAPITests.submit
    write = submissions.ChangeRequestAPITests.write
    baseline = submissions.ChangeRequestAPITests.baseline

    def setUp(self):
        submissions.ChangeRequestAPITests.setUp(self)
        self.owner.name = "確認担当者"
        self.second = models.User(firebase_uid="reviewer2", email="reviewer2@example.com", name="別の確認担当者")
        self.db.add(self.second)
        self.db.flush()
        self.db.add(models.TeamMember(team_id=self.team.id, user_id=self.second.id))
        self.db.commit()
        self.write()
        response = self.submit()
        self.assertEqual(response.status_code, 201, response.text)
        self.original = response.json()
        self.row_id = self.original["id"]
        self.detail = f"{self.review_url}/{self.row_id}"

    def decide(self, action="approve", uid="owner", **options):
        return self.request("POST", self.detail + "/" + action, uid=uid, **options)

    def assert_pending(self):
        self.db.expire_all()
        self.assertEqual(self.db.query(models.ChangeRequestReview).count(), 0)
        self.assertEqual(self.request("GET", self.detail).json()["review_decision"], "PENDING")

    def test_approve_by_other_member_preserves_submission_and_git_data(self):
        # 一時リポジトリにだけmainを用意し、承認前後で一切変わらないことを比較する。
        self.baseline({"base.txt": ("100644", b"shared\n")})
        before = {p.relative_to(self.shared): p.read_bytes() for p in self.shared.rglob("*") if p.is_file()}
        workspace_before = (self.workspace / "code.py").read_bytes()
        response = self.decide()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["review_decision"], "APPROVED")
        self.assertEqual(result["reviewer_id"], self.owner.id)
        self.assertEqual(result["reviewer_name"], "確認担当者")
        self.assertIsNone(result["review_reason"])
        datetime.fromisoformat(result["reviewed_at"])
        for key, value in self.original.items():
            if key not in ("review_decision", "reviewer_id", "reviewer_name", "reviewed_at", "review_reason"):
                self.assertEqual(result[key], value, key)
        after = {p.relative_to(self.shared): p.read_bytes() for p in self.shared.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual((self.workspace / "code.py").read_bytes(), workspace_before)
        self.assertEqual(self.db.query(models.ChangeRequestReview).count(), 1)

    def test_request_changes_saves_trimmed_reason_and_keeps_state_open(self):
        response = self.decide("request-changes", json={"reason": "　説明を追記してください。\nテストも追加してください。　"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["review_decision"], "CHANGES_REQUESTED")
        self.assertEqual(result["review_reason"], "説明を追記してください。\nテストも追加してください。")
        self.assertEqual(result["state"], "OPEN")
        self.assertEqual(result["snapshot_commit"], self.original["snapshot_commit"])

    def test_submitter_cannot_approve_or_request_changes(self):
        for action in ("approve", "request-changes"):
            with self.subTest(action=action):
                self.assertEqual(self.decide(action, uid="member", json={"reason": "修正"} if action != "approve" else {}).status_code, 403)
        self.assert_pending()

    def test_outsider_and_unauthenticated_requests_are_rejected(self):
        for action in ("approve", "request-changes"):
            body = {} if action == "approve" else {"reason": "修正"}
            with self.subTest(action=action):
                self.assertEqual(self.decide(action, uid="outsider", json=body).status_code, 403)
                self.assertEqual(self.decide(action, uid="invalid", json=body).status_code, 401)
                self.assertIn(self.client.post(self.detail + "/" + action, json=body).status_code, (401, 403))
        self.assert_pending()

    def test_other_team_and_other_project_or_missing_request_are_rejected(self):
        other = self.request("POST", json={"name": "別のプロジェクト"}).json()["id"]
        for action in ("approve", "request-changes"):
            body = {} if action == "approve" else {"reason": "修正"}
            with self.subTest(action=action):
                team_url = self.detail.replace(f"/teams/{self.team.id}/", f"/teams/{self.other_team.id}/")
                self.assertEqual(self.request("POST", team_url + "/" + action, uid="owner", json=body).status_code, 403)
                other_url = f"{self.url}/{other}/change-requests/{self.row_id}/{action}"
                self.assertEqual(self.request("POST", other_url, uid="owner", json=body).status_code, 404)
                self.assertEqual(self.request("POST", f"{self.review_url}/99999/{action}", uid="owner", json=body).status_code, 404)
        self.assert_pending()

    def test_closed_pending_request_cannot_be_decided(self):
        row = self.db.get(models.ChangeRequest, self.row_id)
        row.state = "CLOSED"
        self.db.commit()
        self.assertEqual(self.decide().status_code, 409)
        self.assertEqual(self.decide("request-changes", json={"reason": "修正"}).status_code, 409)
        self.assert_pending()

    def test_final_decision_cannot_be_overwritten_even_by_same_retry(self):
        for decision_action in ("approve", "request-changes"):
            # 判定済みの依頼は再利用せず、新しい提出を一件ずつ作る。
            self.row_id = self.submit().json()["id"]
            self.detail = f"{self.review_url}/{self.row_id}"
            body = {} if decision_action == "approve" else {"reason": "修正"}
            first = self.decide(decision_action, json=body)
            self.assertEqual(first.status_code, 200, first.text)
            for uid in ("owner", "reviewer2"):
                for action in ("approve", "request-changes"):
                    with self.subTest(initial=decision_action, uid=uid, action=action):
                        response = self.decide(action, uid=uid, json={} if action == "approve" else {"reason": "別の理由"})
                        self.assertEqual(response.status_code, 409)
            self.assertEqual(self.request("GET", self.detail).json(), first.json())

    def test_new_submission_after_changes_requested_is_pending(self):
        self.assertEqual(self.decide("request-changes", json={"reason": "修正"}).status_code, 200)
        # 提出APIの古いキーを再送しても、判定をPENDINGへ戻してはいけない。
        old_retry = self.submit(self.original["request_key"])
        self.assertEqual(old_retry.status_code, 200)
        self.assertEqual(old_retry.json()["review_decision"], "CHANGES_REQUESTED")
        self.write(text="修正しました\n")
        result = self.submit()
        self.assertEqual(result.status_code, 201)
        self.assertNotEqual(result.json()["id"], self.row_id)
        self.assertEqual(result.json()["review_decision"], "PENDING")
        self.assertEqual(self.request("GET", self.detail).json()["review_decision"], "CHANGES_REQUESTED")

    def test_list_and_detail_include_review_fields_and_existing_fields(self):
        before = self.request("GET", self.detail).json()
        self.assertEqual(before["review_decision"], "PENDING")
        self.assertIsNone(before["reviewer_id"])
        self.assertIsNone(before["reviewed_at"])
        self.assertEqual(self.decide().status_code, 200)
        detailed = self.request("GET", self.detail).json()
        listed = self.request("GET", self.review_url).json()[0]
        self.assertEqual(detailed, listed)
        for key in ("title", "description", "state", "snapshot_commit", "base_commit", "git_ref", "created_at", "updated_at"):
            self.assertEqual(detailed[key], before[key])

    def test_reasons_and_untrusted_identity_fields_are_validated(self):
        for body in ({}, {"reason": ""}, {"reason": "　 \n\t"}, {"reason": "a" * 2001},
                     {"reason": None}, {"reason": "bad\0reason"}, {"reason": "修正", "reviewer_id": self.member.id}):
            with self.subTest(body=body):
                self.assertEqual(self.decide("request-changes", json=body).status_code, 422)
        for body in ({"reviewer_id": self.member.id}, {"reason": "承認理由"}, {"git_ref": "/etc/passwd"}):
            with self.subTest(body=body):
                self.assertEqual(self.decide(json=body).status_code, 422)
        self.assert_pending()
        response = self.decide("request-changes", json={"reason": " " + "あ" * 2000 + " "})
        self.assertEqual(response.status_code, 200, response.text)

    def test_database_error_rolls_back_and_retry_can_decide(self):
        error = OperationalError("private SQL", {}, Exception("postgres://private-secret"))
        with patch.object(self.db, "commit", side_effect=error):
            response = self.decide()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("private", response.text)
        self.assert_pending()
        self.assertFalse(list(self.shared.rglob("*.lock")))
        self.assertEqual(self.decide().status_code, 200)

    def test_commit_succeeded_but_response_failed_is_resolved_via_detail(self):
        # コミット後の再取得だけ失敗させ、再送で二重判定されないことを確認する。
        with patch.object(self.db, "refresh", side_effect=OperationalError("private", {}, Exception())):
            response = self.decide()
        self.assertEqual(response.status_code, 503)
        self.db.expire_all()
        self.assertEqual(self.request("GET", self.detail).json()["review_decision"], "APPROVED")
        self.assertEqual(self.decide().status_code, 409)
        self.assertEqual(self.db.query(models.ChangeRequestReview).count(), 1)

    def test_mixed_git_reference_conflict_blocks_decision_and_preserves_data(self):
        subprocess.run(["git", "--git-dir", str(self.shared), "pack-refs", "--all", "--no-prune"],
                       check=True, capture_output=True, timeout=30, shell=False)
        packed = self.shared / "packed-refs"
        packed.write_bytes(packed.read_bytes().replace(self.original["snapshot_commit"].encode(), self.original["base_commit"].encode()))
        before = packed.read_bytes()
        response = self.decide()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(packed.read_bytes(), before)
        self.assert_pending()

    def test_missing_reference_or_journal_is_not_repaired_by_decision(self):
        head = self.shared / self.original["git_ref"]
        head.rename(head.with_name("saved-head"))
        self.assertEqual(self.decide().status_code, 503)
        self.assertFalse(head.exists())
        head.with_name("saved-head").rename(head)
        journal = next((self.project_dir / "review-journal").glob("*.json"))
        journal.rename(journal.with_suffix(".saved"))
        self.assertEqual(self.decide().status_code, 503)
        self.assertFalse(journal.exists())
        self.assert_pending()

    def test_packed_only_references_can_be_approved_without_creating_loose_refs(self):
        subprocess.run(["git", "--git-dir", str(self.shared), "pack-refs", "--all"],
                       check=True, capture_output=True, timeout=30, shell=False)
        ref = self.shared / self.original["git_ref"]
        self.assertFalse(ref.exists())
        before = (self.shared / "packed-refs").read_bytes()
        self.assertEqual(self.decide().status_code, 200)
        self.assertFalse(ref.exists())
        self.assertEqual((self.shared / "packed-refs").read_bytes(), before)

    def test_git_error_does_not_leave_a_decision_or_disclose_internal_error(self):
        with patch.object(ReviewGit, "run", side_effect=StorageUnavailable("/private/server/path")):
            response = self.decide()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("/private", response.text)
        self.assert_pending()

    def test_snapshot_blob_corruption_is_detected_without_repair(self):
        # このテスト専用Gitのblobだけを破損させる。実Gitデータには接続しない。
        with ReviewGit(self.storage).session(self.project_id) as review:
            entries = review.entries(self.original["snapshot_commit"])
            oid = entries["code.py"][1]
        path = self.shared / "objects" / oid[:2] / oid[2:]
        changed = b"tampered content\n"
        corrupted = zlib.compress(f"blob {len(changed)}\0".encode() + changed)
        path.chmod(0o600)  # 一時fixtureだけを破損可能にし、非rootで実行する試験にも対応する。
        path.write_bytes(corrupted)
        response = self.decide()
        self.assertIn(response.status_code, (409, 503), response.text)
        self.assertEqual(path.read_bytes(), corrupted)
        self.assert_pending()
    def test_snapshot_parent_mismatch_is_detected_even_if_refs_match_database(self):
        # DB・記録・参照が同じでも、基準コミットを親に持たない不正な提出は承認しない。
        from app.change_requests import row_manifest
        with ReviewGit(self.storage).session(self.project_id) as review:
            tree = review.tree({"code.py": ("100644", b"invalid root snapshot\n")})
            invalid = review.run("commit-tree", tree, data=b"No expected parent\n").decode().strip()
        row = self.db.get(models.ChangeRequest, self.row_id)
        row.snapshot_commit = invalid
        self.db.commit()
        (self.shared / row.git_ref).write_text(invalid + "\n")
        journal = next((self.project_dir / "review-journal").glob("*.json"))
        journal.write_text(json.dumps(row_manifest(row)), encoding="utf-8")
        self.assertEqual(self.decide().status_code, 409)
        self.assert_pending()

    def test_decision_uses_submitted_snapshot_after_workspace_is_edited(self):
        self.write(text="not the submitted code\n")
        response = self.decide()
        self.assertEqual(response.status_code, 200, response.text)
        diff = self.request("GET", self.detail + "/diff").json()
        self.assertIn("print('first')", diff["files"][0]["patch"])
        self.assertNotIn("not the submitted code", diff["files"][0]["patch"])

    def test_stale_loaded_pending_state_does_not_overwrite_existing_decision(self):
        # 同じSessionがPENDINGを読み込んだ後に、別Sessionで判定された状況を再現する。
        row = self.db.get(models.ChangeRequest, self.row_id)
        self.assertEqual(row.review_decision, "PENDING")
        team_id, project_id, request_id, owner_id = self.team.id, self.project_id, self.row_id, self.owner.id
        with sessionmaker(bind=self.engine)() as other:
            decide_change_request(other, self.storage, team_id, project_id, request_id, owner_id, "APPROVED")
        with self.assertRaises(StorageConflict):
            decide_change_request(self.db, self.storage, team_id, project_id, request_id, self.second.id,
                                  "CHANGES_REQUESTED", "上書き禁止")
        self.db.expire_all()
        self.assertEqual(self.db.get(models.ChangeRequest, request_id).review_decision, "APPROVED")

    def test_concurrent_approval_and_changes_request_have_one_winner(self):
        team_id, project_id, request_id = self.team.id, self.project_id, self.row_id
        users = [self.owner.id, self.second.id]
        barrier = threading.Barrier(2)

        def decide_once(item):
            user_id, decision = item
            with sessionmaker(bind=self.engine)() as db:
                barrier.wait(timeout=5)
                try:
                    row = decide_change_request(db, self.storage, team_id, project_id, request_id, user_id,
                                                decision, "修正" if decision == "CHANGES_REQUESTED" else None)
                    return "success", row.review_decision
                except StorageConflict:
                    return "conflict", None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide_once, zip(users, ["APPROVED", "CHANGES_REQUESTED"])))
        self.assertEqual([r[0] for r in results].count("success"), 1)
        self.assertEqual([r[0] for r in results].count("conflict"), 1)
        self.db.expire_all()
        self.assertEqual(self.db.query(models.ChangeRequestReview).count(), 1)

    @unittest.skipUnless(os.environ.get("PROJECT_TEST_DATABASE_URL"), "Requires PostgreSQL row locking")
    def test_postgres_row_lock_reads_current_state_after_waiting(self):
        # 手動トランザクションで依頼をロックし、サービスがDB上で実際に待つことを確認する。
        # 別スレッドの古いPENDING判定がCLOSEDを上書きしない検証で、flockだけのテストではない。
        team_id, project_id, request_id, user_id = self.team.id, self.project_id, self.row_id, self.owner.id
        pid_ready = threading.Event()
        worker_pid = []
        with sessionmaker(bind=self.engine)() as blocker:
            locked = blocker.query(models.ChangeRequest).filter_by(id=request_id).with_for_update().one()

            def waiting_reviewer():
                with sessionmaker(bind=self.engine)() as db:
                    worker_pid.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
                    pid_ready.set()
                    try:
                        decide_change_request(db, self.storage, team_id, project_id, request_id, user_id, "APPROVED")
                        return "unexpected success"
                    except StorageConflict:
                        return "conflict"

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(waiting_reviewer)
                blocked = False
                try:
                    self.assertTrue(pid_ready.wait(5))
                    # pg_blocking_pidsで待機を確認し、時間だけに頼って判定しない。
                    import time
                    deadline = time.monotonic() + 8
                    with self.engine.connect() as observer:
                        while time.monotonic() < deadline:
                            blocked = bool(observer.execute(text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": worker_pid[0]}).scalar_one())
                            if blocked:
                                break
                            time.sleep(0.05)
                finally:
                    # 待機確認の失敗時も先にロックを解放し、テスト自身を停止させない。
                    locked.state = "CLOSED"
                    blocker.commit()
                self.assertEqual(future.result(timeout=10), "conflict")
                self.assertTrue(blocked)
        self.assert_pending()


class ChangeRequestReviewMetadataTests(unittest.TestCase):
    """判定結果の一覧・詳細取得はGitに触れず、Windowsでも検証できる。"""
    setUpClass = classmethod(fixtures.ProjectAPITests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectAPITests.tearDownClass.__func__)
    setUp = fixtures.ProjectAPITests.setUp
    tearDown = fixtures.ProjectAPITests.tearDown
    request = fixtures.ProjectAPITests.request
    headers = staticmethod(fixtures.ProjectAPITests.headers)
    decode_token = staticmethod(fixtures.ProjectAPITests.decode_token)

    def test_reviewed_result_is_available_via_existing_metadata_apis(self):
        import uuid
        from app.project_storage import get_project_storage
        project_id = self.request("POST", json={"name": "判定結果"}).json()["id"]
        row = models.ChangeRequest(project_id=project_id, submitted_by=self.member.id,
                                   request_key=str(uuid.uuid4()), title="Metadata", snapshot_commit="a" * 40,
                                   base_commit="b" * 40, git_ref="refs/change-requests/metadata/head")
        self.owner.name = "確認者"
        self.db.add(row)
        self.db.flush()
        self.db.add(models.ChangeRequestReview(change_request_id=row.id, decision="APPROVED",
                                              reviewer_id=self.owner.id, reviewed_at=datetime(2026, 11, 16)))
        self.db.commit()

        def no_storage():
            raise AssertionError("Metadata reads must not inspect Git")

        self.main.app.dependency_overrides[get_project_storage] = no_storage
        try:
            url = f"{self.url}/{project_id}/change-requests"
            result = self.request("GET", f"{url}/{row.id}").json()
            self.assertEqual(result["review_decision"], "APPROVED")
            self.assertEqual(result["reviewer_id"], self.owner.id)
            self.assertEqual(result["reviewer_name"], "確認者")
            self.assertEqual(result["state"], "OPEN")
            self.assertEqual(self.request("GET", url).json(), [result])
        finally:
            self.main.app.dependency_overrides.pop(get_project_storage, None)
