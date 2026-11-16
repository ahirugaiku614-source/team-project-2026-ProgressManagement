"""looseとpackedを混在させる検証。新規の一時Git領域だけを操作する。"""

from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from app.project_storage import ProjectStorage, StorageConflict, StorageUnavailable
from app.review_git import ReviewGit


@unittest.skipUnless(sys.platform == "linux", "Git references require Linux storage")
class ReviewReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = ProjectStorage(str(Path(temporary.name) / "storage"))
        self.storage.initialize(1)
        self.shared = Path(self.storage.root) / "projects/1/shared.git"
        with self.session() as review:
            base = review.main_commit()
            tree = review.tree({"code.py": ("100644", b"code\n")})
            head = review.run("commit-tree", tree, "-p", base, data=b"Test snapshot\n").decode().strip()
            self.manifest = {"ref_key": uuid.uuid4().hex, "base_commit": base, "snapshot_commit": head}
            review.ensure_refs(self.manifest)
        self.ref_name = "refs/change-requests/" + self.manifest["ref_key"]
        self.ref_dir = self.shared / self.ref_name

    @contextmanager
    def session(self):
        with ReviewGit(self.storage).session(1) as review:
            yield review

    def git(self, *args, check=True):
        return subprocess.run(["git", "--git-dir", str(self.shared), *args],
                              capture_output=True, text=True, timeout=5, check=check, shell=False)

    def pack(self, keep_loose=True):
        self.git("pack-refs", "--all", *(["--no-prune"] if keep_loose else []))

    def replace_packed_head(self):
        packed = self.shared / "packed-refs"
        packed.write_bytes(packed.read_bytes().replace(self.manifest["snapshot_commit"].encode(),
                                                     self.manifest["base_commit"].encode()))

    def test_matching_loose_and_packed_refs_remain_untouched(self):
        self.pack()
        paths = [self.ref_dir / "base", self.ref_dir / "head", self.shared / "packed-refs"]
        before = [(p.read_bytes(), p.stat().st_ino) for p in paths]
        with self.session() as review:
            review.verify_refs(self.manifest)
            review.ensure_refs(self.manifest)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_ino) for p in paths])
        self.assertFalse(list(self.shared.rglob("*.lock")))

    def test_loose_cannot_hide_conflicting_packed_review(self):
        self.pack()
        self.replace_packed_head()
        before = (self.shared / "packed-refs").read_bytes()
        loose = (self.ref_dir / "head").read_bytes()
        with self.session() as review:
            for operation in (review.verify_refs, review.ensure_refs):
                with self.assertRaises(StorageConflict):
                    operation(self.manifest)
        self.assertEqual((self.shared / "packed-refs").read_bytes(), before)
        self.assertEqual((self.ref_dir / "head").read_bytes(), loose)

    def test_packed_cannot_hide_conflicting_loose_review(self):
        self.pack()
        head = self.ref_dir / "head"
        head.write_text(self.manifest["base_commit"] + "\n")
        with self.session() as review:
            with self.assertRaises(StorageConflict):
                review.ensure_refs(self.manifest)
        self.assertEqual(head.read_text().strip(), self.manifest["base_commit"])

    def test_packed_only_retry_does_not_create_shadowing_loose_refs(self):
        self.pack(keep_loose=False)
        before = (self.shared / "packed-refs").read_bytes()
        with self.session() as review:
            review.ensure_refs(self.manifest)
            review.verify_refs(self.manifest)
        self.assertFalse((self.ref_dir / "base").exists())
        self.assertFalse((self.ref_dir / "head").exists())
        self.assertEqual((self.shared / "packed-refs").read_bytes(), before)

    def test_each_ref_can_use_a_different_representation(self):
        self.pack()
        # テスト用ファイルを退避し、base=packed / head=looseのみの組み合わせを作る。
        (self.ref_dir / "base").rename(self.ref_dir / "saved-base")
        packed = self.shared / "packed-refs"
        packed.write_bytes(b"\n".join(line for line in packed.read_bytes().split(b"\n")
                                   if not line.endswith((self.ref_name + "/head").encode())))
        with self.session() as review:
            review.verify_refs(self.manifest)
            review.ensure_refs(self.manifest)
        self.assertFalse((self.ref_dir / "base").exists())

    def test_existing_head_conflict_does_not_repair_missing_base(self):
        (self.ref_dir / "base").rename(self.ref_dir / "saved-base")
        (self.ref_dir / "head").write_text(self.manifest["base_commit"] + "\n")
        with self.session() as review:
            with self.assertRaises(StorageConflict):
                review.ensure_refs(self.manifest)
        self.assertFalse((self.ref_dir / "base").exists())

    def test_mutable_main_keeps_git_loose_precedence(self):
        main = self.shared / "refs/heads/main"
        main.write_text(self.manifest["base_commit"] + "\n")
        self.pack()
        main.write_text(self.manifest["snapshot_commit"] + "\n")
        expected = self.git("rev-parse", "refs/heads/main").stdout.strip()
        with self.session() as review:
            self.assertEqual(review.main_commit(), expected)
        self.assertEqual(expected, self.manifest["snapshot_commit"])

    def test_unrelated_unicode_reference_and_peeled_tag_are_supported(self):
        self.git("update-ref", "refs/heads/日本語", self.manifest["base_commit"])
        self.git("-c", "user.name=Test", "-c", "user.email=test@localhost", "tag", "-a", "v1",
                 self.manifest["snapshot_commit"], "-m", "annotated tag")
        self.pack(keep_loose=False)
        self.assertIn(b"\n^", (self.shared / "packed-refs").read_bytes())
        with self.session() as review:
            review.verify_refs(self.manifest)
            review.ensure_refs(self.manifest)

    def test_duplicate_packed_entries_are_rejected_even_when_loose_exists(self):
        self.pack()
        packed = self.shared / "packed-refs"
        original = packed.read_bytes()
        record = (self.manifest["snapshot_commit"] + " " + self.ref_name + "/head\n").encode()
        for oid in (self.manifest["snapshot_commit"], self.manifest["base_commit"]):
            with self.subTest(oid=oid):
                corrupted = original + record.replace(self.manifest["snapshot_commit"].encode(), oid.encode())
                packed.write_bytes(corrupted)
                with self.session() as review:
                    with self.assertRaises(StorageConflict):
                        review.ensure_refs(self.manifest)
                self.assertEqual(packed.read_bytes(), corrupted)

    def test_malformed_packed_data_is_not_hidden_by_loose_refs(self):
        packed = self.shared / "packed-refs"
        values = [b"invalid\n", b"a" * 40 + b" " + (self.ref_name + "/head").encode(),
                  b"^" + b"a" * 40 + b"\n", b"0" * 40 + b" refs/heads/main\n",
                  b"a" * 40 + b" refs/../unsafe\n"]
        for value in values:
            with self.subTest(value=value):
                packed.write_bytes(value)
                with self.session() as review:
                    with self.assertRaises(StorageConflict):
                        review.verify_refs(self.manifest)
                self.assertEqual(packed.read_bytes(), value)

    def test_invalid_loose_ref_does_not_fall_back_to_packed(self):
        self.pack()
        head = self.ref_dir / "head"
        for value in (b"", b"ref: refs/heads/main\n", b"0" * 40 + b"\n", b"a" * 40 + b"\nextra"):
            with self.subTest(value=value):
                head.write_bytes(value)
                with self.session() as review:
                    with self.assertRaises(StorageConflict):
                        review.ensure_refs(self.manifest)
                self.assertEqual(head.read_bytes(), value)

    def test_existing_git_locks_are_preserved(self):
        for path in (self.shared / "packed-refs.lock", self.ref_dir / "base.lock", self.ref_dir / "head.lock"):
            with self.subTest(path=path.name):
                path.write_bytes(b"another process owns this lock")
                with self.session() as review:
                    with self.assertRaises(StorageUnavailable):
                        review.ensure_refs(self.manifest)
                self.assertEqual(path.read_bytes(), b"another process owns this lock")
                path.rename(path.with_name("saved-" + path.name))
        self.assertFalse((self.shared / "packed-refs.lock").exists())

    def test_git_lock_cleanup_preserves_replacement_owned_by_someone_else(self):
        with self.session() as review:
            path = self.shared / "packed-refs.lock"
            with self.assertRaises(StorageConflict):
                with review.git_lock(review.shared, "packed-refs.lock"):
                    path.rename(path.with_name("saved-original-lock"))
                    path.write_bytes(b"replacement lock")
            self.assertEqual(path.read_bytes(), b"replacement lock")

    def test_standard_git_writers_cannot_race_reference_publication(self):
        # 一時参照を退避し、復旧時のwrite_once中に実Gitの競合操作を試す。
        (self.ref_dir / "base").rename(self.ref_dir / "saved-base")
        (self.ref_dir / "head").rename(self.ref_dir / "saved-head")
        with self.session() as review:
            write_once = review.write_once

            def verify_locks_then_write(directory, name, content):
                self.assertTrue((self.shared / "packed-refs.lock").exists())
                self.assertTrue((self.ref_dir / "base.lock").exists())
                self.assertTrue((self.ref_dir / "head.lock").exists())
                packed = self.git("-c", "core.packedRefsTimeout=0", "pack-refs", "--all", check=False)
                update = self.git("update-ref", self.ref_name + "/head", self.manifest["base_commit"], check=False)
                self.assertNotEqual(packed.returncode, 0)
                self.assertNotEqual(update.returncode, 0)
                write_once(directory, name, content)

            with patch.object(review, "write_once", side_effect=verify_locks_then_write):
                review.ensure_refs(self.manifest)
            review.verify_refs(self.manifest)
        self.assertFalse(list(self.shared.rglob("*.lock")))

    def test_failure_during_publication_releases_only_our_locks_and_can_retry(self):
        (self.ref_dir / "head").rename(self.ref_dir / "saved-head")
        with self.session() as review:
            with patch.object(review, "write_once", side_effect=StorageUnavailable("test failure")):
                with self.assertRaises(StorageUnavailable):
                    review.ensure_refs(self.manifest)
            self.assertFalse(list(self.shared.rglob("*.lock")))
            review.ensure_refs(self.manifest)
            review.verify_refs(self.manifest)
