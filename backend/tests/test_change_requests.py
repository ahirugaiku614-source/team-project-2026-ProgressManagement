"""実Gitと隔離DBで検証する。利用者の.git設定や既存ボリュームは使わない。"""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import test_projects as fixtures
from app import models, schemas
from app.change_requests import submit_change_request
from app.project_storage import StorageUnavailable
from app.review_git import ReviewGit, MAX_FILE_BYTES


@unittest.skipUnless(sys.platform == "linux", "Review snapshots require Linux")
class ChangeRequestAPITests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ProjectAPITests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectAPITests.tearDownClass.__func__)
    tearDown = fixtures.ProjectAPITests.tearDown
    request = fixtures.ProjectAPITests.request
    headers = staticmethod(fixtures.ProjectAPITests.headers)
    decode_token = staticmethod(fixtures.ProjectAPITests.decode_token)

    def setUp(self):
        fixtures.ProjectAPITests.setUp(self)
        context = fixtures.ProjectAPITests.storage_fixture(self)
        self.storage = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.project_id = self.request("POST", json={"name": "Review project"}).json()["id"]
        self.review_url = f"{self.url}/{self.project_id}/change-requests"
        self.storage.initialize(self.project_id, self.member.id)
        self.project_dir = Path(self.storage.root) / f"projects/{self.project_id}"
        self.workspace = self.project_dir / f"workspaces/{self.member.id}"
        self.shared = self.project_dir / "shared.git"

    def submit(self, key=None, **values):
        body = {"request_key": key or str(uuid.uuid4()), "title": "変更を確認してください", **values}
        return self.request("POST", self.review_url, json=body)

    def write(self, name="code.py", text="print('first')\n"):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def baseline(self, files):
        # テスト専用リポジトリにだけmainを用意する。本番の提出処理はmainへ書き込まない。
        with ReviewGit(self.storage).session(self.project_id) as review:
            tree = review.tree(files)
            commit = review.run("commit-tree", tree, data=b"Test baseline\n").decode().strip()
            with self.storage._directory(review.shared, "refs") as refs:
                with self.storage._directory(refs, "heads") as heads:
                    review.write_once(heads, "main", (commit + "\n").encode())
            return commit

    def test_submit_creates_immutable_commits_and_refs_without_main(self):
        self.write()
        response = self.submit(title="  初回の提出  ", description="説明")
        self.assertEqual(response.status_code, 201, response.text)
        row = response.json()
        self.assertEqual(row["title"], "初回の提出")
        self.assertEqual(row["submitted_by"], self.member.id)
        self.assertEqual(row["state"], "OPEN")
        self.assertNotEqual(row["snapshot_commit"], row["base_commit"])
        self.assertFalse((self.shared / "refs/heads/main").exists())
        self.assertEqual((self.shared / row["git_ref"]).read_text().strip(), row["snapshot_commit"])
        self.assertEqual((self.shared / row["git_ref"].replace("/head", "/base")).read_text().strip(), row["base_commit"])

    def test_empty_workspace_has_no_changes(self):
        response = self.submit()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("No changes", response.json()["detail"])
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 0)

    def test_workspace_equal_to_main_has_no_changes(self):
        self.baseline({"code.py": ("100644", b"same\n")})
        self.write(text="same\n")
        self.assertEqual(self.submit().status_code, 409)

    def test_snapshot_and_diff_remain_unchanged_after_editing(self):
        self.write(text="original\n")
        row = self.submit().json()
        diff_url = f"{self.review_url}/{row['id']}/diff"
        before = self.request("GET", diff_url).json()
        self.write(text="changed after submission\n")
        self.assertEqual(self.request("GET", diff_url).json(), before)
        self.assertIn("+original", before["files"][0]["patch"])

    def test_list_detail_and_project_filter(self):
        self.write()
        row = self.submit().json()
        other = self.request("POST", json={"name": "Other project"}).json()["id"]
        self.assertEqual(self.request("GET", f"{self.url}/{other}/change-requests").json(), [])
        response = self.request("GET", self.review_url, uid="owner")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["id"] for r in response.json()], [row["id"]])
        self.assertEqual(self.request("GET", f"{self.review_url}/{row['id']}", uid="owner").status_code, 200)
        self.assertEqual(self.request("GET", f"{self.url}/{other}/change-requests/{row['id']}").status_code, 404)
        self.assertEqual(self.request("GET", f"{self.review_url}/99999").status_code, 404)
        self.assertEqual(self.request("GET", self.review_url + "?limit=101").status_code, 422)

    def test_all_endpoints_enforce_authentication_team_and_project(self):
        self.write()
        row = self.submit().json()
        for method, url in (("POST", self.review_url), ("GET", self.review_url),
                            ("GET", f"{self.review_url}/{row['id']}"), ("GET", f"{self.review_url}/{row['id']}/diff")):
            with self.subTest(method=method, url=url):
                body = {"request_key": str(uuid.uuid4()), "title": "No access"}
                self.assertEqual(self.request(method, url, uid="outsider", json=body).status_code, 403)
                self.assertEqual(self.request(method, url, uid="invalid", json=body).status_code, 401)
                self.assertIn(self.client.request(method, url, json=body).status_code, (401, 403))
                foreign = url.replace(f"/teams/{self.team.id}/", f"/teams/{self.other_team.id}/")
                self.assertEqual(self.request(method, foreign, json=body).status_code, 403)

    def test_cannot_submit_another_users_workspace(self):
        self.storage.initialize(self.project_id, self.owner.id)
        owner = self.project_dir / f"workspaces/{self.owner.id}/owner-secret.txt"
        owner.write_text("owner only")
        self.assertEqual(self.submit(submitted_by=self.owner.id).status_code, 422)
        self.assertEqual(self.submit(user_id=self.owner.id).status_code, 422)
        self.assertEqual(self.submit().status_code, 409)
        self.write()
        row = self.submit().json()
        response = self.request("GET", f"{self.review_url}/{row['id']}/diff")
        self.assertNotIn("owner-secret", response.text)

    def test_added_modified_deleted_and_binary_files(self):
        base = self.baseline({"code.py": ("100644", b"old\n"), "deleted.txt": ("100644", b"remove\n")})
        before = (self.shared / "refs/heads/main").read_bytes()
        self.write(text="new\n")
        self.write("added.txt", "added\n")
        (self.workspace / "image.bin").write_bytes(b"\0\xff\x01")
        row = self.submit().json()
        self.assertEqual(row["base_commit"], base)
        diff = self.request("GET", f"{self.review_url}/{row['id']}/diff").json()
        files = {f["path"]: f for f in diff["files"]}
        self.assertEqual(files["code.py"]["change"], "M")
        self.assertIn("-old", files["code.py"]["patch"])
        self.assertEqual(files["deleted.txt"]["change"], "D")
        self.assertEqual(files["added.txt"]["change"], "A")
        self.assertTrue(files["image.bin"]["binary"])
        self.assertIsNone(files["image.bin"]["patch"])
        self.assertEqual((self.shared / "refs/heads/main").read_bytes(), before)

    def test_large_diff_is_explicitly_truncated(self):
        self.write("large.txt", "a" * 300000)
        row = self.submit().json()
        response = self.request("GET", f"{self.review_url}/{row['id']}/diff")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["truncated"])
        self.assertIsNone(response.json()["files"][0]["patch"])

    def test_oversized_file_is_rejected_without_database_row(self):
        (self.workspace / "large").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
        self.assertEqual(self.submit().status_code, 413)
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 0)

    def test_unsafe_files_are_rejected_without_reading_external_contents(self):
        external = self.project_dir / "not-a-workspace-secret"
        external.write_text("do not read this")
        malicious = self.workspace / "attack"
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    malicious.symlink_to(external)
                elif kind == "hardlink":
                    os.link(external, malicious)
                else:
                    os.mkfifo(malicious)
                response = self.submit()
                self.assertEqual(response.status_code, 422, response.text)
                self.assertNotIn("do not read this", response.text)
                malicious.unlink()  # 新しく作ったテスト用の攻撃ファイルだけを片付ける。
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 0)

    def test_unusual_paths_are_rejected_but_leading_dash_is_safe(self):
        for name in ("line\nbreak", "back\\slash"):
            self.write(name)
            self.assertEqual(self.submit().status_code, 422)
            (self.workspace / name).unlink()
        self.write("-option.py")
        self.assertEqual(self.submit().status_code, 201)

    def test_user_git_hooks_config_filters_and_remotes_are_never_executed(self):
        sentinel = self.project_dir / "hook-executed"
        hook = self.workspace / ".git/hooks"
        hook.mkdir()
        script = hook / "pre-commit"
        script.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
        script.chmod(0o755)
        (self.workspace / ".git/config").write_text("[include]\npath=/definitely-not-a-git-config\n[remote \"origin\"]\nurl=ssh://invalid.example/repo\n")
        self.write(".gitattributes", "*.py filter=evil diff=evil\n")
        self.write()
        with patch.dict(os.environ, {"GIT_CONFIG_COUNT": "999", "GIT_DIR": "/invalid", "GIT_SSH_COMMAND": "bad"}):
            response = self.submit()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(sentinel.exists())

    def test_idempotent_retry_returns_existing_row_and_rejects_different_payload(self):
        self.write()
        key = str(uuid.uuid4())
        first = self.submit(key).json()
        self.write(text="later\n")
        retry = self.submit(key)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json(), first)
        self.assertEqual(self.submit(key, title="different").status_code, 409)
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 1)

    def test_database_failure_retains_original_snapshot_for_retry(self):
        self.write(text="before failure\n")
        key = str(uuid.uuid4())
        with patch.object(self.db, "commit", side_effect=IntegrityError("test", {}, Exception())):
            self.assertEqual(self.submit(key).status_code, 503)
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 0)
        self.write(text="after failure\n")
        with patch.object(ReviewGit, "capture", side_effect=AssertionError("must recover the original snapshot")):
            retry = self.submit(key)
        self.assertEqual(retry.status_code, 201, retry.text)
        diff = self.request("GET", f"{self.review_url}/{retry.json()['id']}/diff").json()
        self.assertIn("before failure", diff["files"][0]["patch"])
        self.assertNotIn("after failure", diff["files"][0]["patch"])

    def test_git_failure_after_journal_is_recovered_without_resnapshot(self):
        self.write()
        key = str(uuid.uuid4())
        with patch.object(ReviewGit, "ensure_refs", side_effect=StorageUnavailable("test Git failure")):
            self.assertEqual(self.submit(key).status_code, 503)
        self.write(text="edited after failure")
        with patch.object(ReviewGit, "capture", side_effect=AssertionError("must reuse journal")):
            self.assertEqual(self.submit(key).status_code, 201)

    def test_missing_reference_is_recovered_but_mismatched_reference_is_preserved(self):
        self.write()
        row = self.submit().json()
        ref = self.shared / row["git_ref"]
        original = ref.read_bytes()
        ref.rename(ref.with_name("head-backup"))
        self.assertEqual(self.request("GET", f"{self.review_url}/{row['id']}/diff").status_code, 503)
        self.assertEqual(self.submit(row["request_key"]).status_code, 200)
        self.assertEqual(ref.read_bytes(), original)
        ref.write_text(row["base_commit"] + "\n")
        self.assertEqual(self.submit(row["request_key"]).status_code, 409)
        self.assertEqual(ref.read_text().strip(), row["base_commit"])

    def test_missing_journal_can_be_restored_from_committed_database_row(self):
        self.write()
        row = self.submit().json()
        journal = next((self.project_dir / "review-journal").glob("*.json"))
        original = journal.read_bytes()
        journal.rename(journal.with_suffix(".backup"))
        self.assertEqual(self.submit(row["request_key"]).status_code, 200)
        self.assertEqual(journal.read_bytes(), original)

    def test_changed_workspace_during_capture_is_rejected(self):
        self.write()
        capture = ReviewGit._files
        count = 0

        def capture_and_edit(review, directory):
            nonlocal count
            result = capture(review, directory)
            count += 1
            if count == 1:
                self.write(text="concurrent edit\n")
            return result

        with patch.object(ReviewGit, "_files", capture_and_edit):
            self.assertEqual(self.submit().status_code, 409)
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 0)

    def test_simultaneous_submissions_with_same_key_create_one_row(self):
        self.write()
        payload = schemas.ChangeRequestCreate(request_key=uuid.uuid4(), title="Concurrent")
        project_id, user_id = self.project_id, self.member.id

        def submit_once(_):
            with sessionmaker(bind=self.engine)() as db:
                row, created = submit_change_request(db, self.storage, project_id, user_id, payload)
                return row.id, created

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit_once, range(4)))
        self.assertEqual(len({row_id for row_id, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)

    def test_simultaneous_users_keep_their_own_snapshots(self):
        self.write(text="member contents\n")
        self.storage.initialize(self.project_id, self.owner.id)
        (self.project_dir / f"workspaces/{self.owner.id}/code.py").write_text("owner contents\n")
        payload = schemas.ChangeRequestCreate(request_key=uuid.uuid4(), title="Two users")
        project_id = self.project_id
        users = [self.member.id, self.owner.id]

        def submit_user(user_id):
            with sessionmaker(bind=self.engine)() as db:
                row, _ = submit_change_request(db, self.storage, project_id, user_id, payload)
                return row.id, row.submitted_by, row.git_ref

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit_user, users))
        self.assertEqual(len({ref for _, _, ref in results}), 2)
        for row_id, user_id, _ in results:
            diff = self.request("GET", f"{self.review_url}/{row_id}/diff").json()
            expected = "member contents" if user_id == self.member.id else "owner contents"
            self.assertIn(expected, diff["files"][0]["patch"])

    def test_git_gc_keeps_review_objects_and_packed_refs_are_readable(self):
        self.write()
        row = self.submit().json()
        # このテストの一時リポジトリだけにGCを実行し、保持用参照を検証する。
        subprocess.run(["git", "--git-dir", str(self.shared), "gc", "--prune=now"],
                       check=True, capture_output=True, timeout=30, shell=False)
        response = self.request("GET", f"{self.review_url}/{row['id']}/diff")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["snapshot_commit"], row["snapshot_commit"])

    def test_packed_reference_mismatch_is_not_shadowed_by_recovery(self):
        self.write()
        row = self.submit().json()
        subprocess.run(["git", "--git-dir", str(self.shared), "pack-refs", "--all"],
                       check=True, capture_output=True, timeout=30, shell=False)
        packed = self.shared / "packed-refs"
        packed.write_text(packed.read_text().replace(row["snapshot_commit"], row["base_commit"]))
        self.assertEqual(self.submit(row["request_key"]).status_code, 409)
        self.assertFalse((self.shared / row["git_ref"]).exists())

    def test_mixed_reference_conflict_rejects_diff_and_retry_without_database_changes(self):
        self.write()
        row = self.submit().json()
        subprocess.run(["git", "--git-dir", str(self.shared), "pack-refs", "--all", "--no-prune"],
                       check=True, capture_output=True, timeout=30, shell=False)
        packed = self.shared / "packed-refs"
        packed.write_bytes(packed.read_bytes().replace(row["snapshot_commit"].encode(), row["base_commit"].encode()))
        before = packed.read_bytes(), (self.shared / row["git_ref"]).read_bytes()
        self.assertEqual(self.request("GET", f"{self.review_url}/{row['id']}/diff").status_code, 409)
        self.assertEqual(self.submit(row["request_key"]).status_code, 409)
        self.assertEqual(self.request("GET", f"{self.review_url}/{row['id']}").json(), row)
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 1)
        self.assertEqual((packed.read_bytes(), (self.shared / row["git_ref"]).read_bytes()), before)

    def test_shared_config_is_not_used_and_alternates_are_rejected(self):
        self.write()
        (self.shared / "config").write_text("[include]\npath=/invalid/server-file\n")
        self.assertEqual(self.submit().status_code, 201)
        alternate = self.shared / "objects/info/alternates"
        alternate.write_text("/invalid/server-files\n")
        self.assertEqual(self.submit().status_code, 409)

    def test_git_failure_before_journal_preserves_files_and_can_retry(self):
        self.write()
        key = str(uuid.uuid4())
        run = ReviewGit.run

        def fail_hash(review, *args, **kwargs):
            if args[0] == "hash-object":
                raise StorageUnavailable("hash failure")
            return run(review, *args, **kwargs)

        with patch.object(ReviewGit, "run", fail_hash):
            self.assertEqual(self.submit(key).status_code, 503)
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 0)
        self.assertEqual((self.workspace / "code.py").read_text(), "print('first')\n")
        self.assertEqual(self.submit(key).status_code, 201)

    def test_diff_for_files_without_final_newline_has_separate_lines(self):
        self.baseline({"code.py": ("100644", b"old")})
        self.write(text="new")
        row = self.submit().json()
        diff = self.request("GET", f"{self.review_url}/{row['id']}/diff").json()
        patch_text = diff["files"][0]["patch"]
        self.assertIn("-old\n", patch_text)
        self.assertIn("+new\n", patch_text)
        self.assertIn("No newline at end of file", patch_text)

    def test_invalid_input_is_rejected(self):
        for values in ({"title": "　 \t"}, {"title": "a" * 201}, {"description": "a" * 10001},
                       {"request_key": "../bad"}, {"git_command": "push"}, {"description": "bad\0value"}):
            with self.subTest(values=values):
                self.assertEqual(self.submit(**values).status_code, 422)
        body = ('{"request_key":"' + str(uuid.uuid4()) + '","title":"\\ud800"}').encode()
        response = self.client.post(
            self.review_url, content=body, headers={**self.headers(), "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)


class ChangeRequestMetadataTests(unittest.TestCase):
    """一覧・詳細はファイル操作をしないためWindowsでも利用できる。"""
    setUpClass = classmethod(fixtures.ProjectAPITests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectAPITests.tearDownClass.__func__)
    setUp = fixtures.ProjectAPITests.setUp
    tearDown = fixtures.ProjectAPITests.tearDown
    request = fixtures.ProjectAPITests.request
    headers = staticmethod(fixtures.ProjectAPITests.headers)
    decode_token = staticmethod(fixtures.ProjectAPITests.decode_token)

    def test_metadata_endpoints_do_not_require_linux_storage(self):
        from app.project_storage import get_project_storage
        project_id = self.request("POST", json={"name": "Metadata"}).json()["id"]
        row = models.ChangeRequest(project_id=project_id, submitted_by=self.member.id,
                                   request_key=str(uuid.uuid4()), title="Review", snapshot_commit="a" * 40,
                                   base_commit="b" * 40, git_ref="refs/change-requests/test/head")
        self.db.add(row)
        self.db.commit()
        url = f"{self.url}/{project_id}/change-requests"
        self.main.app.dependency_overrides[get_project_storage] = lambda: (_ for _ in ()).throw(AssertionError("must not access disk"))
        try:
            self.assertEqual(self.request("GET", url, uid="owner").status_code, 200)
            response = self.request("GET", f"{url}/{row.id}", uid="owner")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["submitted_by"], self.member.id)
            self.assertEqual(self.request("GET", url, uid="outsider").status_code, 403)
            self.assertEqual(self.request("GET", f"{url}/{row.id}", uid="outsider").status_code, 403)
        finally:
            self.main.app.dependency_overrides.pop(get_project_storage, None)
