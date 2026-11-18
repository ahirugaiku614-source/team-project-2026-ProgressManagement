"""試験専用の永続モック。VM・ディスクはSQLiteの行であり、実ファイルを編集しない。

管理側とは別DBにfenceを記録し、応答消失後も境界側の結果を照合できるようにする。
libvirt、Docker、ネットワーク、シェルを呼ぶ処理やリソース削除操作は存在しない。
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from uuid import UUID, uuid4

from app.editor_contracts import EditorBinding
from vm_manager.contracts import Observation, OwnershipMismatch, StaleFence
from vm_manager.manager import VMManager
from vm_manager.registry import SQLiteRegistry


@contextmanager
def database(path, timeout=10):
    """トランザクション終了と接続のcloseは別。Windowsの一時ファイルを確実に解放する。"""
    db = sqlite3.connect(str(path), timeout=timeout)
    try:
        with db:
            yield db
    finally:
        db.close()


class Clock:
    def __init__(self, now=100000):
        self.now = now

    def __call__(self):
        return self.now


class Members:
    def __init__(self, bindings):
        self.bindings = set((b.team_id, b.project_id, b.user_id) for b in bindings)

    def allowed(self, actor, binding, kind):
        return actor == binding.user_id and (binding.team_id, binding.project_id, actor) in self.bindings


class PersistentFakeDriver:
    def __init__(self, path, clock):
        self.path, self.clock = str(path), clock

    @classmethod
    def create_new(cls, path, clock):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with database(path) as db:
            db.execute("CREATE TABLE resources(vm_id TEXT PRIMARY KEY,disk_id TEXT UNIQUE,owner TEXT,disk_owner TEXT,vm_state TEXT,content TEXT)")
            db.execute("CREATE TABLE fences(vm_id TEXT PRIMARY KEY,generation INTEGER,operation_id TEXT)")
            db.execute("CREATE TABLE calls(operation_id TEXT PRIMARY KEY,fingerprint TEXT,reply TEXT)")
            db.execute("CREATE TABLE events(kind TEXT,vm_id TEXT)")
        return cls(path, clock)

    def rows(self):
        with database(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM resources ORDER BY vm_id")]

    def execute(self, fence):
        binding = fence.command.binding
        owner = binding.model_dump_json()
        operation_id = str(fence.command.operation_id)
        fingerprint = json.dumps([owner, fence.vm_id, fence.disk_id, fence.generation,
                                  fence.command.operation, fence.lease_until_ms, fence.require_vm, fence.require_disk])
        with database(self.path) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            # この検査とモック副作用は同じトランザクション。管理側だけの検査では不十分。
            if self.clock() >= fence.lease_until_ms:
                raise StaleFence("expired_at_driver")
            latest = db.execute("SELECT * FROM fences WHERE vm_id=?", (fence.vm_id,)).fetchone()
            if latest and (latest["generation"] > fence.generation or
                           (latest["generation"] == fence.generation and latest["operation_id"] != operation_id)):
                raise StaleFence("old_driver_generation")
            previous = db.execute("SELECT * FROM calls WHERE operation_id=?", (operation_id,)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise StaleFence("reused_driver_operation")
                reply = json.loads(previous["reply"])
                return Observation(UUID(operation_id), fence.generation, fence.vm_id, fence.disk_id, reply["state"],
                                   binding if reply["vm"] else None, binding if reply["disk"] else None)
            resource = db.execute("SELECT * FROM resources WHERE vm_id=? OR disk_id=?", (fence.vm_id, fence.disk_id)).fetchone()
            if resource and (resource["owner"] != owner or resource["disk_owner"] != owner
                             or resource["vm_id"] != fence.vm_id or resource["disk_id"] != fence.disk_id):
                raise OwnershipMismatch("fake_owner_mismatch")
            if not resource and (fence.require_vm or fence.require_disk):
                raise OwnershipMismatch("expected_resource_missing")
            db.execute("INSERT INTO fences VALUES(?,?,?) ON CONFLICT(vm_id) DO UPDATE SET generation=excluded.generation,operation_id=excluded.operation_id",
                       (fence.vm_id, fence.generation, operation_id))
            kind = fence.command.operation
            if kind == "ENSURE_STARTED":
                if resource is None:
                    db.execute("INSERT INTO resources VALUES(?,?,?,?,'RUNNING','synthetic-edit-data')", (fence.vm_id, fence.disk_id, owner, owner))
                else:
                    db.execute("UPDATE resources SET vm_state='RUNNING' WHERE vm_id=?", (fence.vm_id,))
                db.execute("INSERT INTO events VALUES('START',?)", (fence.vm_id,))
            elif kind == "STOP" and resource:
                db.execute("UPDATE resources SET vm_state='STOPPED' WHERE vm_id=?", (fence.vm_id,))
                db.execute("INSERT INTO events VALUES('STOP',?)", (fence.vm_id,))
            # GET_STATEも新世代を確定してから観測する。古い遅延startは以後受け付けない。
            resource = db.execute("SELECT * FROM resources WHERE vm_id=?", (fence.vm_id,)).fetchone()
            reply = {"state": resource["vm_state"] if resource else "ABSENT", "vm": bool(resource), "disk": bool(resource)}
            db.execute("INSERT INTO calls VALUES(?,?,?)", (operation_id, fingerprint, json.dumps(reply)))
            return Observation(fence.command.operation_id, fence.generation, fence.vm_id, fence.disk_id, reply["state"],
                               binding if resource else None, binding if resource else None)


class LostReply:
    def __init__(self, driver):
        self.driver = driver

    def execute(self, fence):
        self.driver.execute(fence)
        raise TimeoutError("synthetic-secret-must-not-be-exposed")


def concurrent_start(registry_path, driver_path, user, operation_id, barrier, queue):
    """spawnされた別プロセスで同じローカルDBを開く。メモリ内カウンターは共有しない。"""
    binding = EditorBinding(team_id=1, project_id=1, user_id=user)
    clock = Clock()
    manager = VMManager(SQLiteRegistry(registry_path), Members([binding]), PersistentFakeDriver(driver_path, clock), clock, 1000)
    try:
        barrier.wait(timeout=20)
        result = manager.request(user, binding, "ENSURE_STARTED", UUID(operation_id))
        queue.put(("ok", result["environment"]["vm_id"]))
    except Exception as error:
        queue.put((type(error).__name__, str(error)))


def crash_start(registry_path, driver_path, after_effect):
    """専用の試験子プロセスだけを終了し、永続記録からの復旧を検証する。"""
    binding = EditorBinding(team_id=1, project_id=1, user_id=1)
    clock = Clock()
    driver = PersistentFakeDriver(driver_path, clock)
    manager = VMManager(SQLiteRegistry(registry_path), Members([binding]), driver, clock, 1000)
    fence, _ = manager.prepare(1, binding, "ENSURE_STARTED", uuid4())
    if after_effect:
        driver.execute(fence)
    os._exit(73)
