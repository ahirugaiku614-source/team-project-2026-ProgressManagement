"""実行APIから切り離したVM管理の参照ロジック。既定driverは常に拒否する。

枠と操作意図を先に同じDBへ確定し、その後だけdriverへ依頼する。
応答消失やプロセス終了を、未作成と推測して新規作成しない。
driver側のfencing/冪等性も契約として必要であり、管理DBの世代比較だけで代替しない。
"""

import time

from app.editor_contracts import EditorBinding, WorkerCommand
from vm_manager.contracts import (
    BUSY_STATES, CapacityExceeded, Conflict, DisabledDriver, Fence, Forbidden,
    Observation, OwnershipMismatch, StaleFence, disk_key,
)


class VMManager:
    """現在の呼出し元は試験だけ。レジストリと限定ドライバーの間の整合性を管理する。

    authorizeの実装を差し替えても実行APIにはならない。実認証・通信・隔離は別工程。
    lease_msは操作の有効期限であり、VMを止めるタイマーや枠解放の根拠にはしない。
    """
    def __init__(self, registry, authorizer, driver=None, clock_ms=None, lease_ms=30000):
        if type(lease_ms) is not int or not 100 <= lease_ms <= 60000:
            raise ValueError("invalid_lease_duration")
        self.registry, self.authorizer = registry, authorizer
        self.driver = driver if driver is not None else DisabledDriver()
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1000000)
        self.lease_ms = lease_ms

    def authorize(self, actor_user_id, binding, kind):
        """本人確認と現在所属の許可を要求する。照会不能を許可として扱わない。"""
        if type(actor_user_id) is not int or not isinstance(binding, EditorBinding) or actor_user_id != binding.user_id:
            raise Forbidden("owner_required")
        try:
            permitted = self.authorizer.allowed(actor_user_id, binding, kind)
        except Exception:
            raise Forbidden("authorization_unavailable") from None
        if permitted is not True:
            raise Forbidden("authorization_required")

    @staticmethod
    def environment(db, binding):
        """同じUser×ProjectでもTeam/固定IDが変わっていれば操作せず停止する。"""
        row = db.execute("SELECT * FROM environments WHERE project_id=? AND user_id=?",
                         (binding.project_id, binding.user_id)).fetchone()
        if row is not None and (row["team_id"] != binding.team_id or row["vm_id"] != str(binding.environment_key)
                                or row["disk_id"] != disk_key(binding)):
            raise OwnershipMismatch("registry_owner_mismatch")
        return row

    @staticmethod
    def result(db, operation_id):
        operation = dict(db.execute("SELECT * FROM operations WHERE operation_id=?", (str(operation_id),)).fetchone())
        row = db.execute("SELECT * FROM environments WHERE project_id=? AND user_id=?",
                         (operation["project_id"], operation["user_id"])).fetchone()
        # 過去操作の成功と現在の状態は別フィールド。過去startを再送しても停止済みVMを再起動しない。
        return {"operation": operation, "environment": dict(row), "real_execution_enabled": False}

    @staticmethod
    def quarantine(db, row, code):
        """応答不明を失敗・未作成と断定せず記録する。データや予約枠は取り消さない。"""
        if row["active_op"]:
            db.execute("UPDATE operations SET status='UNKNOWN',result_state='QUARANTINED',error_code=? WHERE operation_id=? AND status='ACTIVE'",
                       (code, row["active_op"]))
        db.execute("UPDATE environments SET state='QUARANTINED',active_op=NULL,lease_until_ms=NULL,error_code=? WHERE project_id=? AND user_id=?",
                   (code, row["project_id"], row["user_id"]))
        # QUARANTINEDは枠を保持する。lease切れだけを根拠に空き枠を増やさない。

    def state(self, actor_user_id, binding):
        """管理DBの状態だけを返す。実ワーカーへの問い合わせや再起動は行わない。"""
        self.authorize(actor_user_id, binding, "GET_STATE")
        now = self.clock_ms()
        with self.registry.transaction(now) as db:
            row = self.environment(db, binding)
            if row is None:
                return None
            if row["active_op"] and now >= row["lease_until_ms"]:
                self.quarantine(db, row, "lease_expired")
            return dict(self.environment(db, binding))

    def prepare(self, actor_user_id, binding, kind, operation_id):
        """操作意図と枠を先に永続化する。プロセス停止試験のためdispatchと分離する。

        同じoperation_idはkind/Bindingも同じ場合だけ再利用できる。
        同じUser/Projectでも別の操作IDなら、進行中の操作を横取りしない。
        """
        command = WorkerCommand(operation_id=operation_id, operation=kind, binding=binding)
        self.authorize(actor_user_id, binding, kind)
        now = self.clock_ms()
        with self.registry.transaction(now) as db:
            previous = db.execute("SELECT * FROM operations WHERE operation_id=?", (str(operation_id),)).fetchone()
            row = self.environment(db, binding)
            if previous is not None:
                if (previous["project_id"], previous["user_id"], previous["team_id"], previous["kind"]) != (
                        binding.project_id, binding.user_id, binding.team_id, kind):
                    raise Conflict("operation_id_reused")
                if row is None:
                    raise Conflict("missing_environment_record")
                if row["active_op"] and now >= row["lease_until_ms"]:
                    self.quarantine(db, row, "lease_expired")
                return None, self.result(db, operation_id)
            if row is None:
                if kind == "GET_STATE":
                    raise Conflict("nothing_to_reconcile")
                db.execute("INSERT INTO environments VALUES(?,?,?,?,?,'ABSENT',0,NULL,NULL,0,0,NULL)",
                           (binding.project_id, binding.user_id, binding.team_id, str(binding.environment_key), disk_key(binding)))
                row = self.environment(db, binding)
            if row["active_op"]:
                if kind != "GET_STATE" or now < row["lease_until_ms"]:
                    raise Conflict("operation_in_progress")
                self.quarantine(db, row, "lease_expired")
                row = self.environment(db, binding)
            state = row["state"]
            if state == "QUARANTINED" and kind != "GET_STATE":
                raise Conflict("reconciliation_required")
            if kind == "GET_STATE" and state != "QUARANTINED":
                raise Conflict("nothing_to_reconcile")
            generation = row["generation"] + 1
            if generation >= 2**62:
                raise Conflict("generation_exhausted")
            # RUNNINGは既に一枠を使用。新しい起動だけを、全プロセス共有のDB内で数えて予約する。
            if kind == "ENSURE_STARTED" and state in ("ABSENT", "STOPPED"):
                placeholders = ",".join("?" for _ in BUSY_STATES)
                occupied = db.execute(f"SELECT count(*) FROM environments WHERE state IN ({placeholders})", BUSY_STATES).fetchone()[0]
                limit = db.execute("SELECT max_running FROM meta WHERE id=1").fetchone()[0]
                if occupied >= limit:
                    raise CapacityExceeded("running_limit_reached")
            no_op = (kind == "ENSURE_STARTED" and state == "RUNNING") or (kind == "STOP" and state in ("ABSENT", "STOPPED"))
            next_state = state if no_op else ("PREPARING" if kind == "ENSURE_STARTED" and state == "ABSENT"
                                             else "STARTING" if kind == "ENSURE_STARTED" else "STOPPING" if kind == "STOP" else "QUARANTINED")
            lease = None if no_op else now + self.lease_ms
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)",
                       (str(operation_id), binding.project_id, binding.user_id, binding.team_id, kind, generation,
                        "SUCCEEDED" if no_op else "ACTIVE", next_state, None))
            db.execute("UPDATE environments SET state=?,generation=?,active_op=?,lease_until_ms=?,error_code=NULL WHERE project_id=? AND user_id=?",
                       (next_state, generation, None if no_op else str(operation_id), lease, binding.project_id, binding.user_id))
            fence = None if no_op else Fence(command, row["vm_id"], row["disk_id"], generation, lease,
                                             bool(row["vm_seen"]), bool(row["disk_seen"]))
            return fence, self.result(db, operation_id)

    @staticmethod
    def verify_active(db, fence, now):
        """保存済みの操作・世代・期限・既知リソースがすべて一致するときだけ送信可能。"""
        row = VMManager.environment(db, fence.command.binding)
        if (row is None or row["active_op"] != str(fence.command.operation_id) or row["generation"] != fence.generation
                or row["lease_until_ms"] != fence.lease_until_ms or now >= fence.lease_until_ms):
            raise StaleFence("inactive_or_expired_fence")
        operation = db.execute("SELECT * FROM operations WHERE operation_id=?", (str(fence.command.operation_id),)).fetchone()
        if (operation is None or operation["status"] != "ACTIVE" or operation["kind"] != fence.command.operation
                or operation["generation"] != fence.generation
                or (operation["project_id"], operation["user_id"], operation["team_id"]) !=
                   (row["project_id"], row["user_id"], row["team_id"])):
            raise StaleFence("inactive_operation")
        if fence.require_vm != bool(row["vm_seen"]) or fence.require_disk != bool(row["disk_seen"]):
            raise StaleFence("resource_expectation_changed")
        return row

    def mark_unknown(self, fence, code):
        """ドライバーの生エラーを保存せず固定コードで隔離。遅れた旧世代の失敗も拒否する。"""
        # より新しい照合が先に確定していても、遅れて来た結果で上書きしない。
        with self.registry.transaction(self.clock_ms()) as db:
            row = self.environment(db, fence.command.binding)
            if row is None or row["generation"] != fence.generation or row["active_op"] != str(fence.command.operation_id):
                raise StaleFence("superseded_operation")
            self.quarantine(db, row, code)
            return self.result(db, fence.command.operation_id)

    def dispatch(self, actor_user_id, fence):
        """意図コミット後にだけdriverを呼ぶ。失敗を根拠にVMやdiskを取り消さない。"""
        self.authorize(actor_user_id, fence.command.binding, fence.command.operation)
        try:
            with self.registry.transaction(self.clock_ms()) as db:
                row = self.verify_active(db, fence, self.clock_ms())
                if row["state"] == "PREPARING":
                    db.execute("UPDATE environments SET state='STARTING' WHERE project_id=? AND user_id=?",
                               (row["project_id"], row["user_id"]))
                    db.execute("UPDATE operations SET result_state='STARTING' WHERE operation_id=?", (str(fence.command.operation_id),))
        except StaleFence:
            # 古いdispatchはdriverまで届かせない。枠解放も自動では行わない。
            raise
        try:
            observation = self.driver.execute(fence)
            if not isinstance(observation, Observation):
                raise Conflict("invalid_driver_reply")
            observation.verify(fence)
        except Exception as error:
            code = "owner_mismatch" if isinstance(error, OwnershipMismatch) else "stale_driver_fence" if isinstance(error, StaleFence) else "driver_uncertain"
            return self.mark_unknown(fence, code)
        return self.complete(actor_user_id, fence, observation)

    def complete(self, actor_user_id, fence, observation):
        """遅れた応答・期限切れ・失効を確認し直す。成功していても不明なら隔離保持。"""
        observation.verify(fence)
        try:
            self.authorize(actor_user_id, fence.command.binding, fence.command.operation)
        except Forbidden:
            # 副作用後の失効でも枠は保全するが、失効した呼出し元へ結果の詳細を返さない。
            self.mark_unknown(fence, "authorization_lost")
            raise
        now = self.clock_ms()
        with self.registry.transaction(now) as db:
            row = self.environment(db, fence.command.binding)
            if row is None or row["generation"] != fence.generation or row["active_op"] != str(fence.command.operation_id):
                raise StaleFence("superseded_operation")
            operation = db.execute("SELECT * FROM operations WHERE operation_id=?", (str(fence.command.operation_id),)).fetchone()
            # 完了経路でもDBに確定した封筒を照合する。期限だけ延長した偽の応答は受理しない。
            if (operation is None or row["lease_until_ms"] != fence.lease_until_ms or operation["status"] != "ACTIVE"
                    or operation["kind"] != fence.command.operation
                    or operation["generation"] != fence.generation
                    or (operation["project_id"], operation["user_id"], operation["team_id"]) !=
                       (row["project_id"], row["user_id"], row["team_id"])
                    or fence.require_vm != bool(row["vm_seen"]) or fence.require_disk != bool(row["disk_seen"])):
                raise StaleFence("completion_fence_mismatch")
            error = None
            if now >= fence.lease_until_ms:
                error = "lease_expired"
            elif (row["vm_seen"] and observation.vm_owner is None) or (row["disk_seen"] and observation.disk_owner is None):
                error = "known_resource_missing"
            elif fence.command.operation == "ENSURE_STARTED" and observation.state != "RUNNING":
                error = "start_unconfirmed"
            elif fence.command.operation == "STOP" and observation.state not in ("STOPPED", "ABSENT"):
                error = "stop_unconfirmed"
            if error:
                self.quarantine(db, row, error)
            else:
                db.execute("UPDATE environments SET state=?,active_op=NULL,lease_until_ms=NULL,error_code=NULL,vm_seen=max(vm_seen,?),disk_seen=max(disk_seen,?) WHERE project_id=? AND user_id=?",
                           (observation.state, int(observation.vm_owner is not None), int(observation.disk_owner is not None),
                            row["project_id"], row["user_id"]))
                db.execute("UPDATE operations SET status='SUCCEEDED',result_state=?,error_code=NULL WHERE operation_id=?",
                           (observation.state, str(fence.command.operation_id)))
            return self.result(db, fence.command.operation_id)

    def request(self, actor_user_id, binding, kind, operation_id):
        """意図保存が成功した新規操作だけdispatchし、再送は永続記録を返す。"""
        fence, result = self.prepare(actor_user_id, binding, kind, operation_id)
        return result if fence is None else self.dispatch(actor_user_id, fence)
