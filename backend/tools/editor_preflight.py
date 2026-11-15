"""Read-only checks for the proposed editor worker baseline.

This is an operator tool, not an API, launcher, security certification or feature
flag. It never creates containers or changes Docker/volume/permission settings.
Run on the intended worker: python tools/editor_preflight.py
"""

import json
import subprocess
import sys


class ProbeUnavailable(Exception):
    pass


def collect_docker_info():
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=True,
            shell=False,
        )
        info = json.loads(result.stdout)
        if not isinstance(info, dict):
            raise ValueError("Invalid info format")
        return info
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        # Never print Docker output, daemon endpoints, host paths or credentials.
        raise ProbeUnavailable("Dockerの状態を取得できませんでした。実行環境と接続権限を確認してください。") from None


def evaluate_worker(info):
    """Check the chosen baseline; passing is NOT evidence of tenant isolation."""
    if not isinstance(info, dict):
        raise ProbeUnavailable("Dockerの応答形式が不正です。")
    options = info.get("SecurityOptions")
    if not isinstance(options, list):
        options = []
    option_values = [value for value in options if isinstance(value, str)]
    options = {value.split(",", 1)[0] for value in option_values}
    runtimes = info.get("Runtimes")
    if not isinstance(runtimes, dict):
        runtimes = {}
    checks = {
        "linux_engine": info.get("OSType") == "linux",
        "rootless_engine": "name=rootless" in options or "rootless" in options,
        "runsc_registered": "runsc" in runtimes,
        "seccomp_enabled": any(
            value.split(",", 1)[0] == "name=seccomp"
            and ("profile=builtin" in value.split(",") or "profile=default" in value.split(","))
            for value in option_values
        ),
        "cgroup_v2_systemd": str(info.get("CgroupVersion")) == "2" and info.get("CgroupDriver") == "systemd",
        "memory_limit_available": info.get("MemoryLimit") is True,
        "pids_limit_available": info.get("PidsLimit") is True,
        "cpu_quota_available": info.get("CpuCfsQuota") is True,
    }
    # Deliberately exclude raw info: it includes host paths and other metadata.
    return {
        "status": "requires_validation" if all(checks.values()) else "blocked",
        "checks": checks,
        "editor_launch_enabled": False,
        "message": "実行基盤の事前確認だけです。保存領域・認証・WebSocket・隔離の実機検証が別途必要です。",
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        report = evaluate_worker(collect_docker_info())
    except ProbeUnavailable as error:
        report = {"status": "unavailable", "editor_launch_enabled": False, "message": str(error)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "requires_validation" else 2


if __name__ == "__main__":
    raise SystemExit(main())
