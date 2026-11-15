"""Trusted diagnostic only. Does not start code-server or expose a service.

Run inside a disposable, non-root, read-only, network=none container with no
data volumes or secrets. Passing does not certify hostile-code containment.
"""

import json
import os
from pathlib import Path


def inspect_sandbox():
    status = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()
    cgroup = Path("/sys/fs/cgroup")
    memory = (cgroup / "memory.max").read_text().strip()
    pids = (cgroup / "pids.max").read_text().strip()
    quota, period = (cgroup / "cpu.max").read_text().split()
    checks = {
        "non_root": os.getuid() != 0 and os.getgid() != 0 and 0 not in os.getgroups(),
        "no_capabilities": int(status["CapEff"], 16) == 0,
        "no_new_privileges": status["NoNewPrivs"] == "1",
        "seccomp_filter": status["Seccomp"] == "2",
        "loopback_only": {p.name for p in Path("/sys/class/net").iterdir()} == {"lo"},
        "read_only_root": bool(os.statvfs("/").f_flag & os.ST_RDONLY),
        "memory_bounded": memory.isdigit() and 0 < int(memory) <= 134217728,
        "processes_bounded": pids.isdigit() and 0 < int(pids) <= 32,
        "cpu_bounded": quota.isdigit() and 0 < int(quota) / int(period) <= 0.5,
        "no_known_docker_socket": not any(Path(p).exists() for p in (
            "/var/run/docker.sock", "/run/docker.sock", "/run/user/1000/docker.sock",
        )),
        "no_application_credentials": not any(Path(p).exists() for p in (
            "/app/firebase-credentials.json", "/app/.env", "/app/backend/firebase-credentials.json",
        )),
    }
    return {"checks": checks, "diagnostics_passed": all(checks.values()), "editor_launch_enabled": False}


if __name__ == "__main__":
    try:
        result = inspect_sandbox()
    except (OSError, ValueError, KeyError):
        result = {"diagnostics_passed": False, "editor_launch_enabled": False, "error": "probe_unavailable"}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["diagnostics_passed"] else 2)
