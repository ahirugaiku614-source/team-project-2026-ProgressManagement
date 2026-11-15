import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from app.project_storage import ProjectStorage


@unittest.skipUnless(sys.platform == "linux", "Requires Linux directory permissions")
class EditorStorageBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "storage"
        self.store = ProjectStorage(str(self.root))
        self.store.initialize(1, 1)
        self.workspace = self.root / "projects/1/workspaces/1"

    def test_existing_initialization_preserves_ownership_permissions_and_data(self):
        data = self.workspace / "keep.py"
        data.write_text("print('keep')")
        paths = [self.workspace, data, self.workspace / ".git", self.root / "projects/1/shared.git"]
        before = [(p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode)) for p in paths]
        self.assertEqual(stat.S_IMODE(self.workspace.stat().st_mode), 0o700)
        self.store.initialize(1, 1)
        after = [(p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode)) for p in paths]
        self.assertEqual(before, after)
        self.assertEqual(data.read_text(), "print('keep')")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "Requires disposable root test container")
    def test_nonroot_editor_cannot_write_default_backend_owned_workspace(self):
        # Drop privileges in a child only. cwd is entered before dropping UID,
        # so this tests the workspace itself, not permissions of its ancestors.
        code = """
import os
os.setgroups([])
os.setgid(1000)
os.setuid(1000)
try:
    open('must-not-exist.py', 'x').close()
except PermissionError:
    print('permission_denied')
else:
    raise SystemExit('unexpected_write_access')
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=self.workspace,
                                capture_output=True, text=True, timeout=10, check=True, shell=False)
        self.assertEqual(result.stdout.strip(), "permission_denied")
        self.assertFalse((self.workspace / "must-not-exist.py").exists())
        self.assertEqual(self.workspace.stat().st_uid, 0)
        self.assertEqual(stat.S_IMODE(self.workspace.stat().st_mode), 0o700)
