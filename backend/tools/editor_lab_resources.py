"""診断専用リソースの作成意図を記録し、結果不明時は削除せず保全する。

アプリのAPIやワーカーではない。Dockerへの操作はテストから渡された固定引数だけ。
再起動後のCLIはreport専用で、ジャーナルを入力しての自動削除機能を設けない。
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

LABEL = "org.team-progress.editor-lab-run"


class LabFailure(RuntimeError):
    pass


def valid_name(run_id, kind, name):
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        return False
    suffix = r"(?:user-a|user-b|other-project|shared)" if kind == "volume" else r"container-[0-9a-f]{32}"
    return kind in ("volume", "container") and isinstance(name, str) and bool(re.fullmatch("editor-lab-" + run_id + "-" + suffix, name))


def identity(info, kind, run_id, name):
    """名前とラベルに加え、コンテナID/ボリューム作成時刻を所有権の確認に使う。"""
    labels = info.get("Labels", {}) if kind == "volume" else info.get("Config", {}).get("Labels", {})
    actual_name = info.get("Name", "").removeprefix("/")
    if not valid_name(run_id, kind, name) or actual_name != name or labels.get(LABEL) != run_id:
        raise ValueError("Resource ownership does not match")
    stamp = info.get("CreatedAt") if kind == "volume" else info.get("Id")
    if not isinstance(stamp, str) or not stamp:
        raise ValueError("Resource identity is unavailable")
    return stamp


class LabResources:
    def __init__(self, docker, directory=None, announce=True):
        self.docker = docker
        self.run_id = uuid.uuid4().hex
        self.directory = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="editor-lab-journal-"))
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / (self.run_id + ".json")
        self.record = {"version": 1, "run_id": self.run_id, "daemon_id": None, "resources": []}
        self.persist()
        if announce:
            print(f"Editor lab run={self.run_id} journal={self.path}", file=sys.stderr)
        self.record["daemon_id"] = self.docker("info", "--format", "{{.ID}}")
        if not self.record["daemon_id"]:
            raise self.failure()
        self.persist()

    def failure(self):
        return LabFailure(f"Diagnostic resources preserved; run={self.run_id}; journal={self.path}")

    def persist(self):
        # 作成意図をDocker操作より先に保存する。ホストの秘密・パス・環境変数・生のstderrは保存しない。
        stage = self.path.with_suffix(".pending-" + uuid.uuid4().hex)
        with stage.open("x", encoding="utf-8") as stream:
            json.dump(self.record, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, self.path)
        if os.name == "posix":
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def check_daemon(self):
        if self.docker("info", "--format", "{{.ID}}") != self.record["daemon_id"]:
            raise self.failure()

    def inspect(self, item):
        self.check_daemon()
        info = json.loads(self.docker(item["kind"], "inspect", item["name"]))[0]
        stamp = identity(info, item["kind"], self.run_id, item["name"])
        if item.get("identity") is not None and stamp != item["identity"]:
            raise self.failure()
        return info, stamp

    def create(self, kind, name, args):
        if not valid_name(self.run_id, kind, name):
            raise self.failure()
        item = {"kind": kind, "name": name, "state": "PLANNED"}
        self.record["resources"].append(item)
        self.persist()  # 応答を失っても、試みた正確な名前を残す。
        try:
            self.check_daemon()
            listing = [kind, "ls", *( ["-a"] if kind == "container" else [] ), "--filter",
                       "name=^" + ("/" if kind == "container" else "") + name + "$", "--format", "{{.Name}}" if kind == "volume" else "{{.Names}}"]
            if self.docker(*listing):
                raise ValueError("Pre-existing name")
            response = self.docker(*args)
            info, stamp = self.inspect(item)
            if (kind == "volume" and response != name) or (kind == "container" and response != stamp):
                raise ValueError("Create reply does not identify this resource")
            item.update(state="CONFIRMED", identity=stamp)
            self.persist()
        except Exception:
            # timeoutでも実際には作られた可能性がある。推測で採用・削除しない。
            item["state"] = "UNCERTAIN"
            self.persist()
            raise self.failure() from None
        return name

    def volume(self, role):
        name = "editor-lab-" + self.run_id + "-" + role
        return self.create("volume", name, ["volume", "create", "--driver", "local", "--label", LABEL + "=" + self.run_id, name])

    def container(self, args):
        name = "editor-lab-" + self.run_id + "-container-" + uuid.uuid4().hex
        return self.create("container", name, ["create", "--name", name, "--label", LABEL + "=" + self.run_id, *args])

    def confirmed(self, kind, name):
        return any(i["kind"] == kind and i["name"] == name and i["state"] == "CONFIRMED" for i in self.record["resources"])

    def cleanup(self):
        # 作成結果不明・所有権不一致・接続先変更・実行中が一つでもあれば、全体を保全する。
        # 関連コンテナが存在するかもしれないボリュームを先に消すことも避ける。
        pending = [i for i in self.record["resources"] if i["state"] != "REMOVED"]
        if any(i["state"] != "CONFIRMED" for i in pending):
            raise self.failure()
        try:
            for item in pending:
                info, _ = self.inspect(item)
                if item["kind"] == "container" and info.get("State", {}).get("Running") is not False:
                    raise ValueError("Resource is running or state is unknown")
            ordered = sorted(pending, key=lambda i: i["kind"] != "container")
            for item in ordered:
                self.inspect(item)  # 削除直前にもID・ラベルを照合。ホスト管理者の同時改ざんは保証外。
                self.check_daemon()
                # コンテナは不変IDで削除し、同名への差替えで別物を消さない。
                target = item["identity"] if item["kind"] == "container" else item["name"]
                self.docker(item["kind"], "rm", target)  # force、stop、pruneは使用しない。
                item["state"] = "REMOVED"
                self.persist()
        except Exception:
            raise self.failure() from None


def report(path, docker):
    """永続ジャーナルにある正確な名前だけを読み取る。削除やラベル全件検索はしない。"""
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    run_id = saved["run_id"]
    if saved.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Invalid journal")
    # デーモン停止中も、ローカルに残る計画名を報告できるようにする。
    # 接続不能を「残存なし」と解釈したり、別デーモンを自動で探したりしない。
    try:
        context_matches = docker("info", "--format", "{{.ID}}") == saved["daemon_id"]
        context_status = "matches" if context_matches else "context_mismatch"
    except Exception:
        context_matches, context_status = False, "context_unavailable"
    rows = []
    for item in saved["resources"]:
        if not valid_name(run_id, item["kind"], item["name"]):
            raise ValueError("Invalid resource name")
        state = context_status
        if context_matches:
            try:
                info = json.loads(docker(item["kind"], "inspect", item["name"]))[0]
                stamp = identity(info, item["kind"], run_id, item["name"])
                state = "ownership_matches" if item.get("identity", stamp) == stamp else "identity_mismatch"
            except (ValueError, KeyError, TypeError):
                state = "ownership_mismatch"
            except Exception:
                state = "not_found_or_unavailable"  # 接続失敗を不存在と断定しない。
        rows.append({"kind": item["kind"], "name": item["name"], "recorded_state": item["state"], "observation": state})
    return {"run_id": run_id, "context_status": context_status, "resources": rows,
            "read_only": True, "automatic_deletion_enabled": False}


if __name__ == "__main__":
    def read_only_docker(*args):
        if args != ("info", "--format", "{{.ID}}") and not (
            len(args) == 3 and args[0] in ("volume", "container") and args[1] == "inspect"
        ):
            raise ValueError("Read-only operation required")
        return subprocess.run(["docker", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, encoding="utf-8", timeout=15, shell=False).stdout.strip()
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--report":
            raise ValueError("Use --report JOURNAL")
        print(json.dumps(report(sys.argv[2], read_only_docker), ensure_ascii=False, indent=2))
    except Exception:
        print(json.dumps({"error": "report_unavailable", "automatic_deletion_enabled": False}))
        raise SystemExit(2)
