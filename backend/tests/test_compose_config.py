import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ComposeConfigTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("docker"), "Docker CLI is required for Compose validation")
    def test_backend_and_database_receive_matching_configuration(self):
        # Explicit empty env file and synthetic values: never load the real .env.
        env = dict(os.environ, POSTGRES_USER="test_user", POSTGRES_PASSWORD="test@:/#%$ password",
                   POSTGRES_DB="test_db", CODE_SERVER_PASSWORD="test-code-server-only")
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "empty.env"
            env_file.write_text("", encoding="utf-8")
            result = subprocess.run(
                ["docker", "compose", "--env-file", str(env_file), "config", "--format", "json"],
                cwd=Path(__file__).parents[2], env=env, capture_output=True, text=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, "Compose config validation failed")
        services = json.loads(result.stdout)["services"]
        backend = services["backend"]["environment"]
        for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            self.assertTrue(backend[name] == services["db"]["environment"][name], name)
            # Rendered Compose config escapes $ as $$ so it can be reused as YAML.
            self.assertTrue(backend[name].replace("$$", "$") == env[name], name)
        self.assertEqual(backend["POSTGRES_HOST"], "db")
        self.assertEqual(backend["POSTGRES_PORT"], "5432")
        self.assertEqual(backend["PROJECT_STORAGE_ROOT"], "/var/lib/team-projects")
        mounts = services["backend"]["volumes"]
        self.assertTrue(any(m["type"] == "volume" and m["source"] == "project_storage"
                            and m["target"] == "/var/lib/team-projects" for m in mounts))
        for name in ("frontend", "code-server"):
            self.assertFalse(any(m.get("source") == "project_storage" for m in services[name]["volumes"]))
