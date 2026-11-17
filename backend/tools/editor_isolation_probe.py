"""新規の診断用ボリュームだけを使い、非root・限定マウント・保持を実測する。

任意コードやcode-serverは実行しない。ホストの絶対パスや秘密を要求しない。
ここで成功しても、コンテナ脱出耐性や本番利用者間の隔離の証明にはならない。
"""

import json
import os
from pathlib import Path
import subprocess
import sys

from editor_sandbox_probe import inspect_sandbox

ROLES = ("user-a", "user-b", "other-project", "shared")
ROOT = Path("/workspace")


def git(*arguments, data=b""):
    # 新しい診断用bareだけが対象。設定・フック・リモート・ホストのHOMEは信用しない。
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "Editor Lab", "GIT_AUTHOR_EMAIL": "lab@localhost", "GIT_AUTHOR_DATE": "@0 +0000",
           "GIT_COMMITTER_NAME": "Editor Lab", "GIT_COMMITTER_EMAIL": "lab@localhost", "GIT_COMMITTER_DATE": "@0 +0000"}
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
                           "--git-dir=/workspace/shared.git", *arguments], input=data, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True, shell=False).stdout


def probe(role, mode):
    if role not in ROLES or mode not in ("seed", "edit", "verify"):
        raise ValueError("Unsupported diagnostic")
    checks = inspect_sandbox()["checks"]
    marker = ROOT / ".diagnostic-owner.json"
    if not marker.exists():
        if mode == "verify":
            raise ValueError("Missing diagnostic fixture")
        record = {"role": role}
        if role == "shared":
            git("init", "--bare", "--initial-branch=main", "--template=", "/workspace/shared.git")
            tree = git("mktree").decode().strip()
            record["main"] = git("commit-tree", tree, data=b"Diagnostic shared main\n").decode().strip()
            git("update-ref", "refs/heads/main", record["main"], "0" * 40)
        else:
            with (ROOT / (role + ".txt")).open("x") as stream:
                stream.write("fixture:" + role + "\n")
        # 初期化済み領域に別のroleを割り当てず、再実行でも元のファイルを上書きしない。
        with marker.open("x") as stream:
            json.dump(record, stream)
    record = json.loads(marker.read_text())
    checks["volume_identity_matches"] = record["role"] == role
    if role == "shared":
        checks["shared_main_unchanged"] = git("rev-parse", "refs/heads/main").decode().strip() == record["main"]
    else:
        own_file = ROOT / (role + ".txt")
        expected = "fixture:" + role + "\n"
        if role == "user-a" and mode == "edit" and own_file.read_text() == expected:
            with own_file.open("a") as stream:
                stream.write("edited\n")
        checks["own_file_readable"] = own_file.read_text() == expected + ("edited\n" if role == "user-a" else "")
        # 自分以外のボリュームと共有Gitはマウントされていない。推測した名前も存在しない。
        checks["other_volumes_not_visible"] = all(not (ROOT / (other + ".txt")).exists() for other in ROLES if other != role)
        checks["shared_repository_not_visible"] = not (ROOT / "shared.git").exists()
        checks["backend_storage_not_visible"] = not Path("/var/lib/team-projects").exists()
        # ターミナル相当の子プロセスも同じ権限・マウント制約を継承する。
        child = """import os
from pathlib import Path
assert not Path('/var/run/docker.sock').exists()
assert not Path('/var/lib/team-projects').exists()
try:
    os.setuid(0)
except PermissionError:
    pass
else:
    raise SystemExit(2)
try:
    open('/etc/editor-lab-escape', 'x').close()
except OSError:
    pass
else:
    raise SystemExit(3)
"""
        result = subprocess.run([sys.executable, "-B", "-c", child], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, shell=False)
        checks["child_process_restrictions"] = result.returncode == 0
        # 他のGitが未マウントなので、固定パスで直接mainを更新しようとしても失敗する。
        try:
            git("update-ref", "refs/heads/main", "1" * 40)
        except subprocess.CalledProcessError:
            checks["cannot_update_shared_main"] = not (ROOT / "shared.git").exists()
        else:
            checks["cannot_update_shared_main"] = False
    return {"role": role, "checks": checks, "diagnostics_passed": all(checks.values()), "editor_launch_enabled": False}


if __name__ == "__main__":
    try:
        result = probe(*sys.argv[1:])
    except Exception:
        # 生のDocker/Git出力、ファイル内容、ホスト情報は報告しない。
        result = {"diagnostics_passed": False, "editor_launch_enabled": False, "error": "diagnostic_failed"}
    print(json.dumps(result))
    raise SystemExit(0 if result["diagnostics_passed"] else 2)
