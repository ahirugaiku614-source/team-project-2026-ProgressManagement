"""一時Git・専用試験DBだけで統合と復旧を検証する。稼働中の保存領域は使わない。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import errno
import subprocess
import sys
import unittest
import zlib
from unittest.mock import patch

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

import test_projects as fixtures
import test_change_request_reviews as reviews
from app import models
from app.change_request_integration import integrate_change_request
from app.integration_git import IntegrationJournal
from app import change_request_integration as service
from app.project_storage import StorageConflict, StorageUnavailable
from app.review_git import ReviewGit


@unittest.skipUnless(sys.platform == "linux", "Integration requires Linux FD-based storage")
class ChangeRequestIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ProjectAPITests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectAPITests.tearDownClass.__func__)
    tearDown = fixtures.ProjectAPITests.tearDown
    request = fixtures.ProjectAPITests.request
    headers = staticmethod(fixtures.ProjectAPITests.headers)
    decode_token = staticmethod(fixtures.ProjectAPITests.decode_token)
    write = reviews.ChangeRequestReviewAPITests.write
    submit = reviews.ChangeRequestReviewAPITests.submit
    baseline = reviews.ChangeRequestReviewAPITests.baseline
    decide = reviews.ChangeRequestReviewAPITests.decide

    def setUp(self):
        reviews.ChangeRequestReviewAPITests.setUp(self)
        response = self.decide()
        self.assertEqual(response.status_code, 200, response.text)
        self.approved = response.json()

    def integrate(self, **options):
        return self.request("POST", self.detail + "/integrate", uid=options.pop("uid", "owner"), **options)

    def status(self):
        return self.request("GET", self.detail + "/integration", uid="owner")

    def main_oid(self):
        with ReviewGit(self.storage).session(self.project_id) as review:
            return review.read_ref(("heads", "main"))

    def git(self, *args):
        return subprocess.run(["git", "--git-dir", str(self.shared), *args], check=True,
                              capture_output=True, timeout=30, shell=False).stdout

    def approve_new(self, text="new version\n"):
        self.write(text=text)
        row = self.submit().json()
        self.row_id = row["id"]
        self.detail = f"{self.review_url}/{self.row_id}"
        response = self.decide()
        self.assertEqual(response.status_code, 200, response.text)
        self.approved = response.json()
        return row

    def fail_completion(self):
        """1回目のPREPAREDは保存し、2回目のCOMPLETEDコミットだけ失敗させる。"""
        original = self.db.commit
        count = 0

        def commit():
            nonlocal count
            count += 1
            if count == 2:
                raise OperationalError("/private/database", {}, Exception())
            original()

        return patch.object(self.db, "commit", side_effect=commit)

    def test_first_integration_keeps_virtual_base_as_parent_and_existing_apis(self):
        self.assertIsNone(self.status().json())
        self.assertIsNone(self.main_oid())
        workspace_before = {p.relative_to(self.workspace): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()}
        refs_before = {p.relative_to(self.shared): p.read_bytes() for p in (self.shared / "refs/change-requests").rglob("*") if p.is_file()}
        diff_before = self.request("GET", self.detail + "/diff").json()
        response = self.integrate(json={})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIsNone(result["main_before"])
        self.assertEqual(result["main_after"], self.approved["snapshot_commit"])
        self.assertEqual(result["integrated_by"], self.owner.id)
        self.assertIsNotNone(result["integrated_at"])
        self.assertEqual(self.main_oid(), self.approved["snapshot_commit"])
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", "main").decode().strip().split(),
                         [self.approved["snapshot_commit"], self.approved["base_commit"]])
        self.assertEqual(self.git("rev-list", "--count", "main").strip(), b"2")
        self.assertEqual(self.request("GET", self.detail).json(), self.approved)
        self.assertEqual(self.request("GET", self.detail + "/diff").json(), diff_before)
        self.assertEqual(workspace_before, {p.relative_to(self.workspace): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()})
        self.assertEqual(refs_before, {p.relative_to(self.shared): p.read_bytes() for p in (self.shared / "refs/change-requests").rglob("*") if p.is_file()})
        self.assertEqual(self.status().json(), result)

    def test_normal_next_integration_and_retry_after_main_advances(self):
        first = self.integrate().json()
        first_url = self.detail
        self.approve_new()
        second = self.integrate()
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["main_before"], first["main_after"])
        self.assertEqual(self.request("POST", first_url + "/integrate", uid="member").json(), first)
        self.assertEqual(self.main_oid(), second.json()["main_after"])

    def test_unapproved_changes_requested_closed_and_invalid_self_approval_are_rejected(self):
        review = self.db.query(models.ChangeRequestReview).one()
        # 変更するのは試験DBの記録のみ。実APIから承認を書き換える機能はない。
        for state, decision, reviewer in (("OPEN", "CHANGES_REQUESTED", self.owner.id),
                                          ("CLOSED", "APPROVED", self.owner.id),
                                          ("OPEN", "APPROVED", self.member.id)):
            with self.subTest(state=state, decision=decision, reviewer=reviewer):
                row = self.db.get(models.ChangeRequest, self.row_id)
                row.state, review.decision, review.reviewer_id = state, decision, reviewer
                review.reason = "修正" if decision == "CHANGES_REQUESTED" else None
                self.db.commit()
                self.assertEqual(self.integrate().status_code, 409)
                self.assertIsNone(self.main_oid())
        self.db.delete(review)
        self.db.get(models.ChangeRequest, self.row_id).state = "OPEN"
        self.db.commit()
        self.assertEqual(self.integrate().status_code, 409)

    def test_auth_team_project_and_unknown_request_are_checked_for_post_and_get(self):
        other = self.request("POST", json={"name": "Other"}).json()["id"]
        for method, suffix in (("POST", "/integrate"), ("GET", "/integration")):
            with self.subTest(method=method):
                url = self.detail + suffix
                self.assertEqual(self.request(method, url, uid="outsider").status_code, 403)
                self.assertEqual(self.request(method, url, uid="invalid").status_code, 401)
                self.assertIn(self.client.request(method, url).status_code, (401, 403))
                self.assertEqual(self.request(method, url.replace(f"/teams/{self.team.id}/", f"/teams/{self.other_team.id}/")).status_code, 403)
                self.assertEqual(self.request(method, f"{self.url}/{other}/change-requests/{self.row_id}{suffix}").status_code, 404)
                self.assertEqual(self.request(method, f"{self.review_url}/99999{suffix}").status_code, 404)
        self.assertIsNone(self.status().json())

    def test_user_ids_commands_and_paths_cannot_be_supplied(self):
        for value in ({"user_id": self.member.id}, {"path": "/secret"}, {"command": "push"}, {"operation_id": "x"}):
            self.assertEqual(self.integrate(json=value).status_code, 422)
        # 統合者が提出者でも、別メンバーの承認があれば統合できる。
        self.assertEqual(self.integrate(uid="member").json()["integrated_by"], self.member.id)

    def test_main_baseline_mismatch_preserves_every_shared_file(self):
        self.baseline({"other.txt": ("100644", b"different\n")})
        before = {p.relative_to(self.shared): p.read_bytes() for p in self.shared.rglob("*") if p.is_file()}
        self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(before, {p.relative_to(self.shared): p.read_bytes() for p in self.shared.rglob("*") if p.is_file()})
        self.assertIsNone(self.status().json())

    def test_initial_main_is_rejected_if_baseline_is_not_virtual_empty_commit(self):
        self.baseline({"old": ("100644", b"history")})
        self.approve_new()
        # 本番でmainを消す処理はない。専用fixtureを未作成に見せ、誤った初回処理を試す。
        (self.shared / "refs/heads/main").rename(self.shared / "saved-main")
        self.assertEqual(self.integrate().status_code, 409)
        self.assertIsNone(self.main_oid())

    def test_retry_is_idempotent_even_for_another_current_member(self):
        first = self.integrate()
        self.assertEqual(first.status_code, 200, first.text)
        for uid in ("owner", "reviewer2", "member"):
            self.assertEqual(self.integrate(uid=uid).json(), first.json())
        self.assertEqual(self.db.query(models.ChangeRequestIntegration).count(), 1)

    def test_git_lock_conflicts_never_remove_existing_locks(self):
        for name in ("packed-refs.lock", "refs/heads/main.lock", self.approved["git_ref"] + ".lock"):
            with self.subTest(name=name):
                lock = self.shared / name
                lock.write_text("other process")
                self.assertEqual(self.integrate().status_code, 503)
                self.assertEqual(lock.read_text(), "other process")
                lock.unlink()  # テストで作ったロックだけを除く。サービスは強制解除しない。
        self.assertIsNone(self.status().json())

    def test_packed_only_and_mixed_main_follow_git_loose_precedence(self):
        self.integrate()
        self.approve_new()
        self.git("pack-refs", "--all")
        packed = (self.shared / "packed-refs").read_bytes()
        self.assertFalse((self.shared / "refs/heads/main").exists())
        second = self.integrate()
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual((self.shared / "packed-refs").read_bytes(), packed)
        self.assertEqual(self.git("rev-parse", "main").decode().strip(), second.json()["main_after"])
        # packedに一つ前のmain、looseに最新版がある状態から、さらに次の統合を行う。
        self.approve_new("third\n")
        third = self.integrate()
        self.assertEqual(third.status_code, 200, third.text)
        self.assertEqual(third.json()["main_before"], second.json()["main_after"])
        self.assertEqual((self.shared / "packed-refs").read_bytes(), packed)

    def test_mixed_review_refs_mismatch_is_not_repaired(self):
        self.git("pack-refs", "--all", "--no-prune")
        packed = self.shared / "packed-refs"
        packed.write_bytes(packed.read_bytes().replace(self.approved["snapshot_commit"].encode(), self.approved["base_commit"].encode()))
        before = packed.read_bytes()
        self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(packed.read_bytes(), before)
        self.assertIsNone(self.main_oid())

    def test_corrupted_blob_cannot_be_integrated(self):
        with ReviewGit(self.storage).session(self.project_id) as review:
            oid = review.entries(self.approved["snapshot_commit"])["code.py"][1]
        path = self.shared / "objects" / oid[:2] / oid[2:]
        path.chmod(0o600)
        content = zlib.compress(b"blob 7\0changed")
        path.write_bytes(content)
        self.assertIn(self.integrate().status_code, (409, 503))
        self.assertEqual(path.read_bytes(), content)
        self.assertIsNone(self.main_oid())

    def test_unsafe_main_files_and_journal_paths_are_rejected(self):
        main = self.shared / "refs/heads/main"
        secret = self.project_dir / "server-private"
        secret.write_text("server secret")
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    main.symlink_to(secret)
                elif kind == "hardlink":
                    os.link(secret, main)
                else:
                    os.mkfifo(main)
                self.assertEqual(self.integrate().status_code, 409)
                self.assertEqual(secret.read_text(), "server secret")
                main.unlink()
        journal = self.project_dir / "integration-journal"
        if journal.exists():
            journal.rename(self.project_dir / "saved-journal")
        journal.symlink_to(self.workspace, target_is_directory=True)
        self.assertEqual(self.integrate().status_code, 409)

    def test_db_failure_before_preparation_leaves_git_untouched(self):
        with patch.object(self.db, "commit", side_effect=OperationalError("private", {}, Exception())):
            response = self.integrate()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.assertIsNone(self.main_oid())
        self.assertIsNone(self.status().json())
        self.assertEqual(self.integrate().status_code, 200)

    def test_git_update_failure_after_preparation_can_retry(self):
        real = service.locked_main

        @contextmanager
        def failure(review):
            with real(review):
                def publish(*args):
                    raise StorageUnavailable("private git failure")
                yield publish

        with patch.object(service, "locked_main", failure):
            self.assertEqual(self.integrate().status_code, 503)
        prepared = self.status().json()
        self.assertEqual(prepared["status"], "PREPARED")
        self.assertIsNone(self.main_oid())
        response = self.integrate()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["operation_id"], prepared["operation_id"])

    def test_db_failure_after_git_update_recovers_without_rewriting_main(self):
        with self.fail_completion():
            response = self.integrate()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.assertEqual(self.main_oid(), self.approved["snapshot_commit"])
        prepared = self.status().json()
        self.assertEqual(prepared["status"], "PREPARED")
        with patch("app.integration_git.os.replace", side_effect=AssertionError("Must not rewrite main")):
            response = self.integrate()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["operation_id"], prepared["operation_id"])
        self.assertEqual(self.request("GET", self.detail).json(), self.approved)

    def test_other_integrator_cannot_take_over_prepared_operation(self):
        with self.fail_completion():
            self.assertEqual(self.integrate().status_code, 503)
        self.assertEqual(self.integrate(uid="member").status_code, 409)
        self.assertEqual(self.integrate().status_code, 200)

    def test_response_or_archival_failure_after_commit_returns_same_result_on_retry(self):
        with patch.object(IntegrationJournal, "archive", side_effect=StorageUnavailable("private path")):
            self.assertEqual(self.integrate().status_code, 503)
        completed = self.status().json()
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(self.integrate().json(), completed)
        self.assertFalse((self.project_dir / "integration-journal/active.json").exists())

    def test_missing_or_altered_journal_after_git_update_requires_operator(self):
        with self.fail_completion():
            self.integrate()
        active = self.project_dir / "integration-journal/active.json"
        saved = active.read_bytes()
        active.rename(active.with_suffix(".saved"))
        self.assertEqual(self.integrate().status_code, 409)
        self.assertFalse(active.exists())
        active.write_bytes(saved.replace(b'"version": 1', b'"version": 9'))
        self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(self.main_oid(), self.approved["snapshot_commit"])

    def test_changed_main_after_uncertain_commit_is_not_rolled_back(self):
        with self.fail_completion():
            self.integrate()
        main = self.shared / "refs/heads/main"
        # 別の管理操作がmainを変更した状況を、一時fixtureだけで再現する。
        main.write_text(self.approved["base_commit"] + "\n")
        # この値は初回の期待値Noneとも統合後headとも異なるため、復旧を推測しない。
        self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(main.read_text().strip(), self.approved["base_commit"])

    def test_another_request_is_blocked_until_prepared_operation_is_recovered(self):
        with self.fail_completion():
            self.integrate()
        old_detail = self.detail
        self.approve_new()
        self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(self.request("POST", old_detail + "/integrate", uid="owner").status_code, 200)
        self.assertEqual(self.integrate().status_code, 200)

    def test_active_journal_without_matching_db_row_is_never_overwritten(self):
        with self.fail_completion():
            self.integrate()
        active = self.project_dir / "integration-journal/active.json"
        content = json.loads(active.read_bytes())
        content["operation_id"] = "00000000-0000-4000-8000-000000000000"
        active.write_text(json.dumps(content))
        before = active.read_bytes()
        self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(active.read_bytes(), before)

    def test_simultaneous_same_request_returns_one_stable_result(self):
        args = self.team.id, self.project_id, self.row_id, self.owner.id

        def integrate():
            with sessionmaker(bind=self.engine)() as db:
                return integrate_change_request(db, self.storage, *args)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: integrate(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.db.query(models.ChangeRequestIntegration).count(), 1)

    def test_simultaneous_different_requests_do_not_overwrite_main(self):
        first_id = self.row_id
        self.approve_new("competing change\n")
        second_id = self.row_id
        team_id, project_id, user_id = self.team.id, self.project_id, self.owner.id

        def integrate(request_id):
            with sessionmaker(bind=self.engine)() as db:
                try:
                    return integrate_change_request(db, self.storage, team_id, project_id, request_id, user_id)
                except StorageConflict:
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(integrate, (first_id, second_id)))
        self.assertEqual(results.count("conflict"), 1)
        success = next(result for result in results if result != "conflict")
        self.assertEqual(self.main_oid(), success["main_after"])
        self.assertEqual(self.db.query(models.ChangeRequestIntegration).count(), 1)

    def test_missing_main_after_an_earlier_integration_is_not_bootstrapped_again(self):
        self.integrate()
        # 元々mainがなかった時点に作った別提出を用意するには、初期依頼を再提出キーで固定する。
        (self.shared / "refs/heads/main").rename(self.shared / "saved-main")
        self.approve_new()
        self.assertEqual(self.integrate().status_code, 409)
        self.assertIsNone(self.main_oid())

    def test_other_project_repository_and_other_member_workspace_are_unchanged(self):
        other = self.request("POST", json={"name": "Independent"}).json()["id"]
        self.storage.initialize(other, self.owner.id)
        self.storage.initialize(self.project_id, self.owner.id)
        other_root = self.project_dir.parent / str(other)
        owner_workspace = self.project_dir / f"workspaces/{self.owner.id}"
        before = {str(p): p.read_bytes() for root in (other_root, owner_workspace) for p in root.rglob("*") if p.is_file()}
        self.write(text="unsubmitted later edits\n")
        self.assertEqual(self.integrate().status_code, 200)
        self.assertEqual((self.workspace / "code.py").read_text(), "unsubmitted later edits\n")
        self.assertEqual(before, {str(p): p.read_bytes() for root in (other_root, owner_workspace) for p in root.rglob("*") if p.is_file()})

    def test_cas_rechecks_main_and_standard_git_cannot_update_while_locked(self):
        real = service.locked_main

        @contextmanager
        def competing_writer(review):
            with real(review) as publish:
                def checked_publish(expected, new):
                    # 実Gitもmain.lockで拒否される。フックは実行させない。
                    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "--git-dir", str(self.shared),
                                             "update-ref", "refs/heads/main", self.approved["base_commit"]],
                                            capture_output=True, timeout=30, shell=False)
                    self.assertNotEqual(result.returncode, 0)
                    # ロック規約を無視する直接操作を試験fixtureだけで再現し、直前CASで止める。
                    (self.shared / "refs/heads/main").write_text(self.approved["base_commit"] + "\n")
                    publish(expected, new)
                yield checked_publish

        with patch.object(service, "locked_main", competing_writer):
            self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(self.main_oid(), self.approved["base_commit"])
        self.assertEqual(self.status().json()["status"], "PREPARED")

    def test_journal_failure_before_git_update_retains_prepared_identity(self):
        with patch.object(IntegrationJournal, "prepare", side_effect=StorageUnavailable("private")):
            self.assertEqual(self.integrate().status_code, 503)
        prepared = self.status().json()
        self.assertEqual(prepared["status"], "PREPARED")
        self.assertIsNone(self.main_oid())
        result = self.integrate()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["operation_id"], prepared["operation_id"])

    def test_prepared_commit_acknowledgement_loss_recovers_without_new_operation(self):
        original = self.db.commit

        def commit_then_disconnect():
            original()
            raise OperationalError("lost database acknowledgement", {}, Exception())

        with patch.object(self.db, "commit", side_effect=commit_then_disconnect):
            self.assertEqual(self.integrate().status_code, 503)
        prepared = self.status().json()
        self.assertEqual(prepared["status"], "PREPARED")
        self.assertIsNone(self.main_oid())
        response = self.integrate()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["operation_id"], prepared["operation_id"])

    def test_membership_is_rechecked_after_prepared_commit(self):
        real = service.lock_authorized_request
        calls = 0
        team_id, user_id = self.team.id, self.owner.id

        def recheck(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                with sessionmaker(bind=self.engine)() as other:
                    other.query(models.TeamMember).filter_by(team_id=team_id, user_id=user_id).delete()
                    other.commit()
            return real(*args)

        with patch.object(service, "lock_authorized_request", side_effect=recheck):
            self.assertEqual(self.integrate().status_code, 403)
        self.assertIsNone(self.main_oid())
        self.assertEqual(self.db.query(models.ChangeRequestIntegration).one().status, "PREPARED")

    def test_corrupted_database_operation_id_cannot_be_used_as_a_file_path(self):
        self.assertEqual(self.integrate().status_code, 200)
        record = self.db.query(models.ChangeRequestIntegration).one()
        # DBの長さ制約は満たすがUUIDではない値を、専用試験DBだけに入れる。
        record.operation_id = "../" + "x" * 33
        self.db.commit()
        from app.integration_git import canonical_operation_id
        with patch("app.integration_git.os.open", side_effect=AssertionError("Must validate before opening")):
            with self.assertRaises(StorageConflict):
                canonical_operation_id(record.operation_id)
        response = self.integrate()
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("../", response.text)
        self.assertEqual(self.main_oid(), self.approved["snapshot_commit"])

    def test_completed_retry_rejects_missing_rewound_or_unrelated_main(self):
        self.assertEqual(self.integrate().status_code, 200)
        main = self.shared / "refs/heads/main"
        after = main.read_bytes()
        main.rename(self.shared / "saved-main-for-review")
        self.assertEqual(self.integrate().status_code, 409)
        self.assertFalse(main.exists())
        for value in (self.approved["base_commit"], self.baseline({"unrelated": ("100644", b"other") } )):
            main.write_text(value + "\n")
            self.assertEqual(self.integrate().status_code, 409)
            self.assertEqual(main.read_text().strip(), value)
        main.write_bytes(after)
        self.assertEqual(self.integrate().status_code, 200)

    def test_directory_fsync_failure_after_replace_must_be_synced_again_before_completion(self):
        real_fsync = os.fsync
        failed = False
        main = self.shared / "refs/heads/main"

        def fail_after_replace(fd):
            nonlocal failed
            path = os.readlink(f"/proc/self/fd/{fd}")
            if path == str(main.parent) and main.exists() and not failed:
                failed = True
                raise OSError(errno.EIO, "simulated directory persistence failure")
            return real_fsync(fd)

        with patch("app.integration_git.os.fsync", side_effect=fail_after_replace):
            self.assertEqual(self.integrate().status_code, 503)
        self.assertTrue(failed)
        self.assertEqual(self.status().json()["status"], "PREPARED")
        self.assertEqual(main.read_text().strip(), self.approved["snapshot_commit"])

        def fail_sync_of_published_main(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == str(main):
                raise OSError(errno.EIO, "published main still cannot be persisted")
            return real_fsync(fd)

        with patch("app.integration_git.os.fsync", side_effect=fail_sync_of_published_main):
            self.assertEqual(self.integrate().status_code, 503)
        self.assertEqual(self.status().json()["status"], "PREPARED")
        self.assertEqual(self.integrate().status_code, 200)

    def test_prepared_initial_retry_must_revalidate_virtual_base(self):
        self.baseline({"history": ("100644", b"not empty")})
        self.approve_new()
        with patch.object(IntegrationJournal, "prepare", side_effect=StorageUnavailable("stop before journal")):
            self.assertEqual(self.integrate().status_code, 503)
        record = self.db.query(models.ChangeRequestIntegration).one()
        # DB/Gitの不整合を専用fixtureだけで再現。NULL beforeを無条件に初回扱いしない。
        record.main_before = None
        self.db.commit()
        (self.shared / "refs/heads/main").rename(self.shared / "saved-main-for-initial-retry")
        self.assertEqual(self.integrate().status_code, 409)
        self.assertIsNone(self.main_oid())

    def test_prepared_retry_rejects_an_already_archived_operation(self):
        self.assertEqual(self.integrate().status_code, 200)
        record = self.db.query(models.ChangeRequestIntegration).one()
        record.status, record.integrated_at = "PREPARED", None
        self.db.commit()
        main = self.shared / "refs/heads/main"
        main.rename(self.shared / "saved-main-before-stale-db-retry")
        self.assertEqual(self.integrate().status_code, 409)
        self.assertFalse(main.exists())
        self.assertFalse((self.project_dir / "integration-journal/active.json").exists())

    def test_active_journal_fsync_failure_never_allows_main_update_on_retry(self):
        journal = self.project_dir / "integration-journal"
        real_fsync = os.fsync

        def fail_parent_after_active_publish(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == str(journal) and (journal / "active.json").exists():
                raise OSError(errno.EIO, "intent directory cannot be persisted")
            return real_fsync(fd)

        with patch("app.integration_git.os.fsync", side_effect=fail_parent_after_active_publish):
            self.assertEqual(self.integrate().status_code, 503)
        self.assertIsNone(self.main_oid())

        def fail_active_file(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == str(journal / "active.json"):
                raise OSError(errno.EIO, "intent file cannot be persisted")
            return real_fsync(fd)

        with patch("app.integration_git.os.fsync", side_effect=fail_active_file):
            self.assertEqual(self.integrate().status_code, 503)
        self.assertIsNone(self.main_oid())
        self.assertEqual(self.status().json()["status"], "PREPARED")
        self.assertEqual(self.integrate().status_code, 200)

    def test_archive_rename_fsync_failure_requires_archive_resync_on_retry(self):
        journal = self.project_dir / "integration-journal"
        real_fsync = os.fsync

        def fail_archive_directory(fd):
            if (os.readlink(f"/proc/self/fd/{fd}") == str(journal)
                    and not (journal / "active.json").exists() and list(journal.glob("*.json"))):
                raise OSError(errno.EIO, "archive directory cannot be persisted")
            return real_fsync(fd)

        with patch("app.integration_git.os.fsync", side_effect=fail_archive_directory):
            self.assertEqual(self.integrate().status_code, 503)
        completed = self.status().json()
        self.assertEqual(completed["status"], "COMPLETED")
        archived = journal / (completed["operation_id"] + ".json")
        self.assertTrue(archived.exists())

        def fail_archive_file(fd):
            if os.readlink(f"/proc/self/fd/{fd}") == str(archived):
                raise OSError(errno.EIO, "archive file cannot be persisted")
            return real_fsync(fd)

        with patch("app.integration_git.os.fsync", side_effect=fail_archive_file):
            self.assertEqual(self.integrate().status_code, 503)
        self.assertEqual(self.integrate().json(), completed)

    def test_completed_active_is_not_archived_when_main_cannot_prove_result(self):
        with patch.object(IntegrationJournal, "archive", side_effect=StorageUnavailable("stop before archive")):
            self.assertEqual(self.integrate().status_code, 503)
        active = self.project_dir / "integration-journal/active.json"
        before = active.read_bytes()
        (self.shared / "refs/heads/main").rename(self.shared / "main-moved-for-review")
        self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(active.read_bytes(), before)
        self.assertEqual(list(active.parent.glob("*.json")), [active])

    def test_completed_active_of_another_request_requires_original_request_recovery(self):
        with patch.object(IntegrationJournal, "archive", side_effect=StorageUnavailable("stop before archive")):
            self.assertEqual(self.integrate().status_code, 503)
        original_url = self.detail
        self.approve_new()
        self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(self.request("POST", original_url + "/integrate", uid="owner").status_code, 200)
        self.assertEqual(self.integrate().status_code, 200)

    def test_completed_replay_ancestry_is_bounded_and_hash_verified(self):
        self.assertEqual(self.integrate().status_code, 200)
        with ReviewGit(self.storage).session(self.project_id) as review:
            current = self.approved["snapshot_commit"]
            tree = review.run("rev-parse", current + "^{tree}").decode().strip()
            for i in range(3):
                current = review.run("commit-tree", tree, "-p", current, data=f"Later {i}\n".encode()).decode().strip()
        self.git("update-ref", "refs/heads/main", current)
        with patch("app.integration_git.MAX_REPLAY_ANCESTRY", 2):
            self.assertEqual(self.integrate().status_code, 409)
        self.assertEqual(self.integrate().status_code, 200)
        path = self.shared / "objects" / current[:2] / current[2:]
        path.chmod(0o600)
        path.write_bytes(zlib.compress(b"commit 8\0tampered"))
        self.assertIn(self.integrate().status_code, (409, 503))

    def test_initial_virtual_base_rejects_noncanonical_commit_headers_or_message(self):
        from app.integration_git import verify_initial_base
        with ReviewGit(self.storage).session(self.project_id) as review:
            raw = review.run("cat-file", "commit", self.approved["base_commit"])
            verify_initial_base(review, self.approved["base_commit"])
            for content in (raw.replace(b"author Project Review", b"author Different Author"),
                            raw.replace(b"Empty review base", b"Other empty root"),
                            raw.replace(b"\nauthor ", b"\nparent " + self.approved["base_commit"].encode() + b"\nauthor ")):
                oid = review.run("hash-object", "-t", "commit", "-w", "--stdin", data=content).decode().strip()
                with self.assertRaises(StorageConflict):
                    verify_initial_base(review, oid)

    def test_packed_only_main_can_be_resynced_during_prepared_recovery(self):
        with self.fail_completion():
            self.assertEqual(self.integrate().status_code, 503)
        self.git("pack-refs", "--all")
        self.assertFalse((self.shared / "refs/heads/main").exists())
        before = (self.shared / "packed-refs").read_bytes()
        with patch("app.integration_git.os.replace", side_effect=AssertionError("No main rewrite during recovery")):
            response = self.integrate()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.shared / "packed-refs").read_bytes(), before)
        self.assertFalse((self.shared / "refs/heads/main").exists())

    def test_last_window_lock_ignoring_writer_is_outside_guarantee(self):
        # このテストは保証の限界を記録する。直前の値検査後にロックを無視して
        # 書かれた値はrenameで置換され得る。通常Gitライターの排他とは区別する。
        # 改ざんするのは本テスト専用mainだけで、稼働中Gitには接続しない。
        real_replace = os.replace
        main = self.shared / "refs/heads/main"

        def ignore_lock_then_replace(*args, **kwargs):
            subprocess.run([sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')",
                            str(main), self.approved["base_commit"]], check=True, timeout=5, shell=False)
            return real_replace(*args, **kwargs)

        with patch("app.integration_git.os.replace", side_effect=ignore_lock_then_replace):
            self.assertEqual(self.integrate().status_code, 200)
        self.assertEqual(main.read_text().strip(), self.approved["snapshot_commit"])

    def child_operation(self, crash_before_completion=False, crash_point=None):
        """OS終了でfinallyが走らない状況を、専用PGと一時Gitだけで再現する。"""
        import time
        # fork前に親の参照値を取り出す。子は親のDB接続を再利用しない。
        args = self.team.id, self.project_id, self.row_id, self.owner.id
        pid = os.fork()
        if pid == 0:
            try:
                self.engine.dispose(close=False)
                with sessionmaker(bind=self.engine)() as db:
                    if crash_before_completion or crash_point in ("prepared", "completed"):
                        original = db.commit
                        count = 0

                        def commit():
                            nonlocal count
                            count += 1
                            if count == 2:
                                if crash_before_completion:
                                    os._exit(73)  # main公開済み・DB確定前。ロックは意図的に残る。
                            original()
                            if count == 1 and crash_point == "prepared":
                                os._exit(75)
                            if count == 2 and crash_point == "completed":
                                os._exit(76)

                        db.commit = commit
                    if crash_point == "archive":
                        real_archive = IntegrationJournal.archive

                        def archive_then_exit(journal, record):
                            real_archive(journal, record)
                            os._exit(77)

                        with patch.object(IntegrationJournal, "archive", archive_then_exit):
                            integrate_change_request(db, self.storage, *args)
                    else:
                        integrate_change_request(db, self.storage, *args)
                os._exit(74)  # DB確定・ロック解放後、HTTP応答前に停止した状況。
            except BaseException:
                os._exit(99)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.05)
        os.kill(pid, 9)  # 今回forkした試験子プロセスだけを終了する。
        os.waitpid(pid, 0)
        self.fail("Temporary child did not finish")

    @unittest.skipUnless(os.environ.get("PROJECT_TEST_DATABASE_URL"), "Process crash requires isolated PostgreSQL")
    def test_process_exit_after_commit_before_response_replays_completed_result(self):
        self.assertEqual(self.child_operation(), 74)
        completed = self.status().json()
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(self.integrate().json(), completed)

    @unittest.skipUnless(os.environ.get("PROJECT_TEST_DATABASE_URL"), "Process crash requires isolated PostgreSQL")
    def test_process_exit_after_git_leaves_locks_for_operator_without_deleting_them(self):
        self.assertEqual(self.child_operation(crash_before_completion=True), 73)
        self.assertEqual(self.status().json()["status"], "PREPARED")
        self.assertEqual((self.shared / "refs/heads/main").read_text().strip(), self.approved["snapshot_commit"])
        # packed/base/headの標準ロックが残るため、自動で削除せず停止する。
        before = {str(p): p.read_bytes() for p in self.shared.rglob("*.lock")}
        self.assertTrue(before)
        self.assertEqual(self.integrate().status_code, 503)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.shared.rglob("*.lock")})

    @unittest.skipUnless(os.environ.get("PROJECT_TEST_DATABASE_URL"), "Process crash requires isolated PostgreSQL")
    def test_process_exit_immediately_after_prepared_db_commit(self):
        self.assertEqual(self.child_operation(crash_point="prepared"), 75)
        self.assertEqual(self.status().json()["status"], "PREPARED")
        self.assertFalse((self.shared / "refs/heads/main").exists())
        locks = {str(p): p.read_bytes() for p in self.shared.rglob("*.lock")}
        self.assertTrue(locks)
        self.assertEqual(self.integrate().status_code, 503)
        self.assertEqual(locks, {str(p): p.read_bytes() for p in self.shared.rglob("*.lock")})

    @unittest.skipUnless(os.environ.get("PROJECT_TEST_DATABASE_URL"), "Process crash requires isolated PostgreSQL")
    def test_process_exit_immediately_after_completed_db_commit(self):
        self.assertEqual(self.child_operation(crash_point="completed"), 76)
        self.assertEqual(self.status().json()["status"], "COMPLETED")
        self.assertTrue((self.project_dir / "integration-journal/active.json").exists())
        locks = {str(p): p.read_bytes() for p in self.shared.rglob("*.lock")}
        self.assertTrue(locks)
        self.assertEqual(self.integrate().status_code, 503)
        self.assertEqual(locks, {str(p): p.read_bytes() for p in self.shared.rglob("*.lock")})

    @unittest.skipUnless(os.environ.get("PROJECT_TEST_DATABASE_URL"), "Process crash requires isolated PostgreSQL")
    def test_process_exit_immediately_after_journal_archive(self):
        self.assertEqual(self.child_operation(crash_point="archive"), 77)
        result = self.status().json()
        self.assertEqual(result["status"], "COMPLETED")
        journal = self.project_dir / "integration-journal"
        self.assertFalse((journal / "active.json").exists())
        self.assertTrue((journal / (result["operation_id"] + ".json")).exists())
        locks = {str(p): p.read_bytes() for p in self.shared.rglob("*.lock")}
        self.assertEqual(self.integrate().status_code, 503)
        self.assertEqual(locks, {str(p): p.read_bytes() for p in self.shared.rglob("*.lock")})
