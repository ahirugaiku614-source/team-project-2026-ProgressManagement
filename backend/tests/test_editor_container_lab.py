"""明示指定した診断イメージだけで実行する、新規ボリュームのコンテナ試験。

通常の回帰ではスキップする。Dockerソケットをテストコンテナへ渡さず、ホストから実行する。
既存名を引数で受け付けず、毎回UUIDから専用名を作る。後片付けも所有ラベル一致のものだけ。
"""

import json
import os
import subprocess
import unittest

from tools.editor_lab_resources import LabResources


@unittest.skipUnless(os.environ.get("EDITOR_LAB_IMAGE_ID"), "Opt-in diagnostic container test; no editor launch")
class EditorContainerLabTests(unittest.TestCase):
    def docker(self, *args):
        result = subprocess.run(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", timeout=40, shell=False)
        if result.returncode:
            self.fail("Diagnostic Docker operation failed; no raw daemon details displayed")
        return result.stdout.strip()

    def setUp(self):
        self.image = os.environ["EDITOR_LAB_IMAGE_ID"]
        self.assertRegex(self.image, r"^sha256:[0-9a-f]{64}$")
        image = json.loads(self.docker("image", "inspect", self.image))[0]
        self.assertEqual(image["Config"]["Labels"].get("org.team-progress.editor-lab"), "v1")
        self.lab = LabResources(self.docker)
        self.addCleanup(self.lab.cleanup)

    def volume(self, role):
        self.assertIn(role, ("user-a", "user-b", "other-project", "shared"))
        return self.lab.volume(role)

    def container(self, role, mode, volume):
        self.assertTrue(self.lab.confirmed("volume", volume))
        # データは今回作った本人相当のボリューム一つだけ。ネットワーク・公開ポートなし。
        name = self.lab.container(["--user", "1000:1000", "--network", "none", "--ipc", "none", "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                    "--memory", "128m", "--cpus", "0.5", "--pids-limit", "32",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,uid=1000,gid=1000,mode=0700",
                    "--mount", "type=volume,source=" + volume + ",target=/workspace", self.image, role, mode])
        info = json.loads(self.docker("container", "inspect", name))[0]
        self.assertEqual([(m["Name"], m["Destination"]) for m in info["Mounts"] if m["Type"] == "volume"], [(volume, "/workspace")])
        self.assertFalse(info["HostConfig"]["Privileged"])
        self.assertFalse(info["HostConfig"]["PortBindings"])
        self.assertFalse(info["HostConfig"]["Binds"])
        return name

    def start(self, name):
        self.assertTrue(self.lab.confirmed("container", name))
        self.lab.check_daemon()
        report = json.loads(self.docker("start", "-a", name))
        self.assertTrue(report["diagnostics_passed"], report)
        self.assertFalse(report["editor_launch_enabled"])
        return report

    def test_fresh_volume_boundaries_terminal_and_restart_preservation(self):
        volumes = {role: self.volume(role) for role in ("user-a", "user-b", "other-project", "shared")}
        # B/別Project/共有bareを先に作る。Aからはこれらのボリュームを一切マウントしない。
        for role in ("user-b", "other-project", "shared"):
            self.start(self.container(role, "seed", volumes[role]))
        a = self.container("user-a", "edit", volumes["user-a"])
        self.start(a)
        self.start(a)  # 同じ停止済みコンテナを再起動。編集済みファイルが保持されることを確認。
        self.start(self.container("user-a", "verify", volumes["user-a"]))  # 再作成でも同じ保存先を使用。
        for role in ("user-b", "other-project", "shared"):
            self.start(self.container(role, "verify", volumes[role]))  # Aの操作後も内容・mainが不変。
