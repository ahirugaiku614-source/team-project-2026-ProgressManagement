"""最小構成の予約とアイドル停止意図を一時台帳で検証する。実行機能は持たない。

Fence/Observationは既存契約を使うが既存managerを呼ばない。完了証跡は試験モックだけが
供給し、RUNNINGという台帳値も実VMや接続可能状態を意味しない。全実機結果はNOT_RUN。
"""

from dataclasses import asdict
from functools import wraps
import json
import time
from uuid import UUID

from app.editor_contracts import EditorBinding, WorkerCommand
from editor_gateway.contracts import VMRoute
from vm_manager.contracts import Fence, Observation, disk_key
from minimal_editor_lab.contracts import IdleEvidence, LabDenied, Profile, StopAck, StopPlan, StorageEvidence


def safe(function):
    """入力/依存先の生エラーを固定拒否へ変換する。実機の接続先や秘密値を表示しない。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            raise LabDenied() from None
    return wrapped


def evidence_json(evidence):
    """停止証跡を決定的に保存し、再送時のすり替えと一回限りnonceの再利用を検出する。"""
    data = asdict(evidence)
    data["binding"] = evidence.binding.model_dump()
    data["quiescence_id"] = str(evidence.quiescence_id)
    return json.dumps(data, sort_keys=True)


class DenyAuthorizer:
    """既定の所属確認器。実Firebase/DBへ接続せず、本人の申告だけでは許可しない。"""

    def allowed(self, actor, binding, kind):
        """検証器が未接続のため、すべての操作を拒否する。"""
        return False


class ResourceModel:
    """起動予約・停止意図・完了照合を管理する独立モデル。実ドライバーを差す入口もない。"""

    def __init__(self, ledger, authorizer=None, clock_ms=None):
        """専用試験台帳だけを受け取り、既定認可は常時拒否とする。"""
        self.ledger = ledger
        self.authorizer = authorizer if authorizer is not None else DenyAuthorizer()
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1000000)

    def _authorize(self, actor, binding, kind):
        """認証済みactorと現在所属を要求する。User IDをリクエストから解決するAPIはない。"""
        if (type(actor) is not int or not isinstance(binding, EditorBinding) or actor != binding.user_id
                or self.authorizer.allowed(actor, binding, kind) is not True):
            raise LabDenied()

    @staticmethod
    def _environment(db, binding):
        """User×ProjectだけでなくTeam・VM/disk固定IDを照合し、別所有者へ付け替えない。"""
        row = db.execute("SELECT * FROM environments WHERE project_id=? AND user_id=?", (binding.project_id, binding.user_id)).fetchone()
        if row and (row["team_id"] != binding.team_id or row["vm_id"] != str(binding.environment_key) or row["disk_id"] != disk_key(binding)):
            raise LabDenied()
        return row

    @staticmethod
    def _result(db, operation_id):
        """操作履歴と現在の状態を分離して返す。履歴再送で再実行しない。"""
        op = db.execute("SELECT * FROM operations WHERE operation_id=?", (str(operation_id),)).fetchone()
        row = db.execute("SELECT * FROM environments WHERE project_id=? AND user_id=?", (op["project_id"], op["user_id"])).fetchone()
        return {"operation": dict(op), "environment": dict(row), "real_execution": "NOT_RUN", "launch_enabled": False}

    @staticmethod
    def _previous(db, command):
        """同じ操作IDは同じ本人/操作でのみ履歴を返し、別の意味で再利用させない。"""
        op = db.execute("SELECT * FROM operations WHERE operation_id=?", (str(command.operation_id),)).fetchone()
        if op and (op["project_id"], op["user_id"], op["team_id"], op["kind"]) != (
                command.binding.project_id, command.binding.user_id, command.binding.team_id, command.operation):
            raise LabDenied()
        return op

    @staticmethod
    def _fence(command, row, now):
        """新しい世代と30秒leaseを束縛する。期限はVMの停止時刻や枠解放時刻ではない。"""
        generation = row["generation"] + 1
        if generation >= 2**62:
            raise LabDenied()
        return Fence(command, row["vm_id"], row["disk_id"], generation, now + 30000,
                     row["generation"] > 0, row["generation"] > 0)

    @staticmethod
    def _record(db, fence, state, evidence=None):
        """資源予約と操作意図を同じDBへ確定する。外部副作用をこの中で実行しない。"""
        b = fence.command.binding
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,'ACTIVE',?,?)",
                   (str(fence.command.operation_id), b.project_id, b.user_id, b.team_id, fence.command.operation, fence.generation,
                    evidence_json(evidence) if evidence else None, str(evidence.quiescence_id) if evidence else None))
        db.execute("UPDATE environments SET state=?,generation=?,active_op=?,lease_ms=? WHERE project_id=? AND user_id=?",
                   (state, fence.generation, str(fence.command.operation_id), fence.lease_until_ms, b.project_id, b.user_id))

    @safe
    def prepare_start(self, actor, binding, profile, operation_id, storage):
        """初回だけdiskを予約し、停止後は同じdiskを再利用。CPU/RAM/全体枠は原子的に予約する。"""
        command = WorkerCommand(operation_id=operation_id, operation="ENSURE_STARTED", binding=binding)
        self._authorize(actor, binding, command.operation)
        if not isinstance(profile, Profile):
            raise LabDenied()
        now = self.clock_ms()
        with self.ledger.transaction(now) as db:
            row = self._environment(db, binding)
            if self._previous(db, command):
                if not row or (row["cpu"], row["memory_mib"], row["root_gib"], row["data_gib"]) != tuple(asdict(profile).values()):
                    raise LabDenied()
                return None, self._result(db, operation_id)
            if row and row["state"] != "STOPPED":
                raise LabDenied()  # 別IDの二重起動は拒否。UNKNOWN/期限切れも空き扱いしない。
            if (not isinstance(storage, StorageEvidence) or storage.healthy is not True
                    or type(storage.limit_gib) is not int or storage.limit_gib < 0
                    or type(storage.sampled_ms) is not int or not 0 <= now - storage.sampled_ms <= 5000):
                raise LabDenied()
            active = db.execute("SELECT coalesce(sum(cpu),0),coalesce(sum(memory_mib),0),count(*) FROM environments WHERE state!='STOPPED'").fetchone()
            disk = db.execute("SELECT coalesce(sum(root_gib+data_gib),0) FROM environments").fetchone()[0]
            budget = self.ledger.budget
            added_disk = 0 if row else profile.root_gib + profile.data_gib
            if (active[0] + profile.cpu > budget.cpu or active[1] + profile.memory_mib > budget.memory_mib
                    or active[2] + 1 > budget.max_running or disk + added_disk > min(budget.disk_gib, storage.limit_gib)):
                raise LabDenied()
            if row:
                if (row["cpu"], row["memory_mib"], row["root_gib"], row["data_gib"]) != tuple(asdict(profile).values()):
                    raise LabDenied()  # 既存diskを別構成へ暗黙変更しない。
            else:
                # 初回行は同じ未確定トランザクション内でだけ作る。操作と結んでSTARTINGにしてからcommitする。
                db.execute("INSERT INTO environments VALUES(?,?,?,?,?,?,?,?,?,'STOPPED',0,NULL,NULL)",
                           (binding.project_id, binding.user_id, binding.team_id, str(binding.environment_key), disk_key(binding),
                            profile.cpu, profile.memory_mib, profile.root_gib, profile.data_gib))
                row = self._environment(db, binding)
            fence = self._fence(command, row, now)
            self._record(db, fence, "STARTING")
            return fence, self._result(db, operation_id)

    @safe
    def prepare_idle_stop(self, actor, binding, operation_id, evidence):
        """ジョブ静止・保存確認後だけSTOP意図を作る。画面閉鎖/CPU低使用だけでは停止しない。"""
        command = WorkerCommand(operation_id=operation_id, operation="STOP", binding=binding)
        self._authorize(actor, binding, command.operation)
        now = self.clock_ms()
        if not isinstance(evidence, IdleEvidence):
            raise LabDenied()
        with self.ledger.transaction(now) as db:
            row = self._environment(db, binding)
            op = self._previous(db, command)
            if op:
                if op["evidence_json"] != evidence_json(evidence):
                    raise LabDenied()
                return None, self._result(db, operation_id)
            if not row or row["state"] != "RUNNING":
                raise LabDenied()
            evidence.validate(binding, row["generation"], now)
            fence = self._fence(command, row, now)
            self._record(db, fence, "STOPPING", evidence)
            return StopPlan(fence, evidence), self._result(db, operation_id)

    @staticmethod
    def _active(db, fence):
        """現在操作/所有者/世代/leaseと保存済みFenceを照合し、遅延・偽造完了を拒否する。"""
        if not isinstance(fence, Fence):
            raise LabDenied()
        row = ResourceModel._environment(db, fence.command.binding)
        op = ResourceModel._previous(db, fence.command)
        if (row is None or op is None or op["status"] != "ACTIVE" or op["generation"] != fence.generation
                or row["active_op"] != str(fence.command.operation_id) or row["generation"] != fence.generation
                or row["lease_ms"] != fence.lease_until_ms
                or fence.require_vm is not (fence.generation > 1) or fence.require_disk is not (fence.generation > 1)):
            raise LabDenied()
        return row, op

    @safe
    def finish(self, actor, fence, observation, stop_ack=None):
        """モック境界の完了証跡を検査する。保存/停止確認前に予約を解放しない。"""
        self._authorize(actor, fence.command.binding, fence.command.operation)
        if not isinstance(observation, Observation):
            raise LabDenied()
        observation.verify(fence)
        now = self.clock_ms()
        with self.ledger.transaction(now) as db:
            row, op = self._active(db, fence)
            valid = now < fence.lease_until_ms and observation.vm_owner is not None and observation.disk_owner is not None
            if fence.command.operation == "STOP":
                valid = valid and observation.state == "STOPPED" and isinstance(stop_ack, StopAck)
                if valid:
                    saved = json.loads(op["evidence_json"])
                    valid = (stop_ack.observation == observation and stop_ack.saved_ack is True
                             and type(stop_ack.revision) is int and stop_ack.revision == saved["revision"]
                             and type(stop_ack.quiescence_id) is UUID
                             and str(stop_ack.quiescence_id) == saved["quiescence_id"])
            else:
                valid = valid and observation.state == "RUNNING" and stop_ack is None
            if valid:
                db.execute("UPDATE environments SET state=?,active_op=NULL,lease_ms=NULL WHERE project_id=? AND user_id=?",
                           (observation.state, row["project_id"], row["user_id"]))
                db.execute("UPDATE operations SET status='SUCCEEDED' WHERE operation_id=?", (str(fence.command.operation_id),))
            else:
                self._quarantine(db, row)
            return self._result(db, fence.command.operation_id)

    @staticmethod
    def _quarantine(db, row):
        """不明操作を隔離し、CPU/RAM/diskを保持する。強制停止・削除・枠返却はしない。"""
        db.execute("UPDATE operations SET status='UNKNOWN' WHERE operation_id=? AND status='ACTIVE'", (row["active_op"],))
        db.execute("UPDATE environments SET state='QUARANTINED',active_op=NULL,lease_ms=NULL WHERE project_id=? AND user_id=?",
                   (row["project_id"], row["user_id"]))

    @safe
    def uncertain(self, actor, fence):
        """停止失敗/応答消失時の記録。成功を推測して資源を解放する復旧は実装しない。"""
        self._authorize(actor, fence.command.binding, fence.command.operation)
        with self.ledger.transaction(self.clock_ms()) as db:
            row, _ = self._active(db, fence)
            self._quarantine(db, row)
            return self._result(db, fence.command.operation_id)

    @safe
    def state(self, actor, binding):
        """現在状態を読む。期限切れの進行中操作は隔離し、期限切れだけで枠を戻さない。"""
        self._authorize(actor, binding, "GET_STATE")
        now = self.clock_ms()
        with self.ledger.transaction(now) as db:
            row = self._environment(db, binding)
            if row is None:
                return None
            if row["active_op"] and now >= row["lease_ms"]:
                self._quarantine(db, row)
            return dict(self._environment(db, binding))

    @safe
    def unavailable_route(self, actor, binding):
        """既存gateway向けDTOとの型整合だけ示す。模擬RUNNINGでもsafe_readyは常にfalse。"""
        row = self.state(actor, binding)
        if row is None:
            raise LabDenied()
        return VMRoute(binding, binding, row["vm_id"], row["disk_id"], row["generation"], row["state"], False, self.clock_ms())
