import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.project_storage import ProjectStorage, StorageConflict, StorageUnavailable


def initialize_in_process(root):
    return ProjectStorage(root).initialize(1, 1)


@unittest.skipUnless(sys.platform == "linux", "Secure storage requires Linux")
class ProjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "storage"
        self.storage = ProjectStorage(str(self.root))

    def test_status_is_read_only_and_missing(self):
        state = self.storage.status(1, 1)
        self.assertEqual(state["repository_state"], "missing")
        self.assertEqual(state["workspace_state"], "missing")
        self.assertFalse(self.root.exists())

    def test_real_git_repositories_and_origin(self):
        state = self.storage.initialize(1, 2)
        self.assertEqual(state["repository_state"], "ready")
        self.assertEqual(state["workspace_state"], "ready")
        shared = self.root / "projects/1/shared.git"
        workspace = self.root / "projects/1/workspaces/2"
        bare = subprocess.check_output(["git", "--git-dir", str(shared), "rev-parse", "--is-bare-repository"], text=True)
        self.assertEqual(bare.strip(), "true")
        origin = subprocess.check_output(["git", "-C", str(workspace), "remote", "get-url", "origin"], text=True)
        self.assertEqual(origin.strip(), str(shared))
        self.assertEqual((workspace / ".git/HEAD").read_text(), "ref: refs/heads/main\n")
        self.assertEqual((shared / "HEAD").read_text(), "ref: refs/heads/main\n")

    def test_projects_and_users_are_isolated(self):
        for project, user in ((1, 1), (1, 2), (2, 1)):
            self.storage.initialize(project, user)
            work = self.root / f"projects/{project}/workspaces/{user}"
            (work / "code.py").write_text(f"{project}:{user}")
        for project, user in ((1, 1), (1, 2), (2, 1)):
            self.storage.initialize(project, user)
            work = self.root / f"projects/{project}/workspaces/{user}"
            self.assertEqual((work / "code.py").read_text(), f"{project}:{user}")
        self.assertFalse((self.root / "projects/1/shared.git/code.py").exists())

    def test_duplicate_initialization_never_runs_git_again(self):
        expected = self.storage.initialize(1, 1)
        with patch.object(self.storage, "_git", side_effect=AssertionError("must not modify existing repo")):
            self.assertEqual(self.storage.initialize(1, 1), expected)

    def test_simultaneous_threads_initialize_once(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            states = list(pool.map(lambda _: self.storage.initialize(1, 1), range(6)))
        self.assertTrue(all(state["workspace_state"] == "ready" for state in states))
        self.assertFalse(list(self.root.rglob(".staging-*")))

    def test_simultaneous_processes_initialize_once(self):
        with multiprocessing.get_context("spawn").Pool(3) as pool:
            states = pool.map(initialize_in_process, [str(self.root)] * 3)
        self.assertTrue(all(state["workspace_state"] == "ready" for state in states))
        self.assertFalse(list(self.root.rglob(".staging-*")))

    def test_lock_timeout_is_retryable_without_initialization(self):
        with patch("fcntl.flock", side_effect=BlockingIOError()), patch(
            "app.project_storage.time.monotonic", side_effect=[0, 20]
        ):
            with self.assertRaises(StorageUnavailable):
                self.storage.initialize(1)
        self.assertEqual(self.storage.status(1)["repository_state"], "missing")
        self.assertEqual(self.storage.initialize(1)["repository_state"], "ready")

    def test_failed_git_is_not_published_and_can_be_retried(self):
        with patch.object(self.storage, "_git", side_effect=StorageUnavailable("failure")):
            with self.assertRaises(StorageUnavailable):
                self.storage.initialize(1, 1)
        self.assertEqual(self.storage.status(1)["repository_state"], "missing")
        leftovers = list(self.root.rglob(".staging-*"))
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(self.storage.initialize(1, 1)["workspace_state"], "ready")
        self.assertTrue(leftovers[0].is_dir())

    def test_workspace_failure_preserves_shared_repository(self):
        self.storage.initialize(1)
        head = self.root / "projects/1/shared.git/HEAD"
        before = head.read_bytes()
        with patch.object(self.storage, "_git", side_effect=StorageUnavailable("failure")):
            with self.assertRaises(StorageUnavailable):
                self.storage.initialize(1, 1)
        self.assertEqual(head.read_bytes(), before)
        self.assertEqual(self.storage.status(1, 1)["workspace_state"], "missing")
        self.assertEqual(self.storage.initialize(1, 1)["workspace_state"], "ready")

    def test_failed_publication_keeps_staging_and_retry_is_safe(self):
        with patch.object(self.storage, "_publish", side_effect=StorageUnavailable("failure")):
            with self.assertRaises(StorageUnavailable):
                self.storage.initialize(1)
        leftovers = list(self.root.rglob(".staging-*"))
        self.assertEqual(len(leftovers), 1)
        self.assertTrue((leftovers[0] / "HEAD").is_file())
        self.assertEqual(self.storage.status(1)["repository_state"], "missing")
        self.assertEqual(self.storage.initialize(1)["repository_state"], "ready")
        self.assertTrue(leftovers[0].is_dir())

    def test_publication_acknowledgment_failure_can_be_retried(self):
        publish = self.storage._publish

        def publish_then_fail(*args):
            publish(*args)
            raise StorageUnavailable("Response failed after publication")

        with patch.object(self.storage, "_publish", side_effect=publish_then_fail):
            with self.assertRaises(StorageUnavailable):
                self.storage.initialize(1)
        self.assertEqual(self.storage.status(1)["repository_state"], "ready")
        with patch.object(self.storage, "_git", side_effect=AssertionError("must not reinitialize")):
            self.assertEqual(self.storage.initialize(1)["repository_state"], "ready")

    def test_git_failure_and_timeout_are_safe_errors(self):
        self.root.mkdir()
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for error in (FileNotFoundError(), subprocess.TimeoutExpired("git", 30),
                          subprocess.CalledProcessError(1, ["git"], stderr=b"private path")):
                with self.subTest(error=type(error).__name__):
                    with patch("app.project_storage.subprocess.run", side_effect=error):
                        with self.assertRaises(StorageUnavailable) as raised:
                            self.storage._git(fd, "init", ".")
                        self.assertNotIn("private path", str(raised.exception))
        finally:
            os.close(fd)

    def test_existing_incomplete_directory_is_never_overwritten(self):
        path = self.root / "projects/1/shared.git"
        path.mkdir(parents=True)
        (path / "keep.txt").write_text("keep")
        self.assertEqual(self.storage.status(1)["repository_state"], "incomplete")
        with self.assertRaises(StorageConflict):
            self.storage.initialize(1)
        self.assertEqual((path / "keep.txt").read_text(), "keep")

    def test_atomic_publication_cannot_replace_even_empty_directory(self):
        self.root.mkdir()
        (self.root / "stage").mkdir()
        (self.root / "destination").mkdir()
        (self.root / "stage/keep").write_text("keep")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(StorageConflict):
                self.storage._publish(fd, "stage", "destination")
        finally:
            os.close(fd)
        self.assertEqual((self.root / "stage/keep").read_text(), "keep")
        self.assertEqual(list((self.root / "destination").iterdir()), [])

    def test_invalid_ids_and_root_paths_are_rejected(self):
        for value in ("../2", "1/2", "/etc", 0, -1, True):
            with self.subTest(value=value):
                with self.assertRaises(StorageConflict):
                    self.storage.initialize(value, 1)
                with self.assertRaises(StorageConflict):
                    self.storage.initialize(1, value)
        for root in ("relative/path", "/", "/tmp/../etc"):
            with self.assertRaises(StorageUnavailable):
                ProjectStorage(root)

    def test_symlinked_storage_ancestor_is_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StorageConflict):
            self.storage.initialize(1)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_project_workspace_and_git_directory_are_rejected(self):
        self.storage.initialize(1, 1)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        for link in (self.root / "projects/2", self.root / "projects/1/workspaces/2"):
            link.symlink_to(outside, target_is_directory=True)
        for project, user in ((2, 1), (1, 2)):
            with self.assertRaises(StorageConflict):
                self.storage.initialize(project, user)
        # Use another user's otherwise valid workspace to test .git redirection.
        self.storage.initialize(1, 3)
        git = self.root / "projects/1/workspaces/3/.git"
        git.rename(git.with_name("old-git"))
        git.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StorageConflict):
            self.storage.status(1, 3)
        self.assertEqual((outside / "keep").read_text(), "keep")

    def test_symlinked_file_hardlink_and_lock_are_rejected(self):
        self.storage.initialize(1, 1)
        work = self.root / "projects/1/workspaces/1"
        outside = Path(self.temp.name) / "outside"
        outside.write_text("keep")
        link = work / "escape"
        link.symlink_to(outside)
        with self.assertRaises(StorageConflict):
            self.storage.status(1, 1)
        link.unlink()  # Only this test's newly created malicious fixture.
        os.link(outside, link)
        with self.assertRaises(StorageConflict):
            self.storage.status(1, 1)
        lock = self.root / "projects/2"
        lock.mkdir()
        (lock / ".init.lock").symlink_to(outside)
        with self.assertRaises(StorageConflict):
            self.storage.initialize(2)
        self.assertEqual(outside.read_text(), "keep")

    def test_marker_for_another_project_is_incomplete(self):
        self.storage.initialize(1)
        marker = self.root / "projects/1/shared.git/.storage-ready.json"
        marker.write_text(json.dumps({"version": 1, "project_id": 2, "user_id": None}))
        self.assertEqual(self.storage.status(1)["repository_state"], "incomplete")
        with self.assertRaises(StorageConflict):
            self.storage.initialize(1)

    def test_git_arguments_and_environment_are_controlled(self):
        self.root.mkdir()
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch.dict(os.environ, {"GIT_DIR": "/elsewhere", "GIT_CONFIG_COUNT": "1"}), patch(
                "app.project_storage.subprocess.run"
            ) as run:
                self.storage._git(fd, "init", "--bare", ".")
                args, kwargs = run.call_args
                self.assertIsInstance(args[0], list)
                self.assertFalse(kwargs["shell"])
                self.assertNotIn("GIT_DIR", kwargs["env"])
                self.assertNotIn("GIT_CONFIG_COUNT", kwargs["env"])
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        finally:
            os.close(fd)
