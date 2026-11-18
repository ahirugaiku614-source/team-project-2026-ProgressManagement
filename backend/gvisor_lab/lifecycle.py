"""gVisor専用の予約・起動確認・保存停止をモックで検証する。実engineアダプターはない。"""

from copy import deepcopy
from dataclasses import asdict, dataclass
import re
import time
from uuid import UUID

from app.editor_contracts import EditorBinding
from gvisor_lab.policy import Capabilities, Denied, DisabledExecutor, Profile, digest_valid, encode, identities, plan_for, safe, validate_plan


@dataclass(frozen=True)
class SaveProof:
    """現在世代の新規受付凍結・全buffer保存・job静止の契約。モック以外の発行器は未実装。"""

    binding: EditorBinding
    generation: int
    sampled_ms: int
    nonce: UUID
    revision: int
    jobs: int | None
    dirty: bool | None
    flushed: bool | None

    def validate(self, binding, generation, now):
        """欠測・未保存・実行中job・古い証跡では停止予約すら作らない。"""
        if (self.binding != binding or type(self.generation) is not int or self.generation != generation
                or type(self.sampled_ms) is not int or not 0 <= now - self.sampled_ms <= 5000
                or type(self.nonce) is not UUID or type(self.revision) is not int or self.revision <= 0
                or type(self.jobs) is not int or self.jobs != 0 or self.dirty is not False or self.flushed is not True):
            raise Denied()


@dataclass(frozen=True)
class Ticket:
    """設定hash・所有者・操作・世代・lease・保存nonceに束縛した限定操作。任意execではない。"""

    operation_id: UUID
    binding: EditorBinding
    kind: str
    generation: int
    lease_ms: int
    policy_hash: str
    runtime_digest: str
    save_nonce: UUID | None = None
    save_revision: int | None = None

    def __post_init__(self):
        """壊れたFenceとSTART/STOPの取り違えを生成時に拒否する。KVMのFenceには変換しない。"""
        if (type(self.operation_id) is not UUID or not isinstance(self.binding, EditorBinding) or self.kind not in ("START", "STOP")
                or type(self.generation) is not int or not 0 < self.generation < 2**62
                or type(self.lease_ms) is not int or not 0 < self.lease_ms < 2**62
                or type(self.policy_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.policy_hash)
                or not digest_valid(self.runtime_digest)):
            raise Denied()
        if self.kind == "START" and (self.save_nonce is not None or self.save_revision is not None):
            raise Denied()
        if self.kind == "STOP" and (type(self.save_nonce) is not UUID or type(self.save_revision) is not int or self.save_revision <= 0):
            raise Denied()

    def serialized(self):
        """操作台帳との完全一致比較用に、JSONだけで保存できる形へ変換する。"""
        return {**asdict(self), "binding": self.binding.model_dump(), "operation_id": str(self.operation_id),
                "save_nonce": str(self.save_nonce) if self.save_nonce else None}


@dataclass(frozen=True)
class MockObservation:
    """モックのinspect応答。実runsc/health確認の証拠として受け入れる型ではない。"""

    ticket: Ticket
    binding: EditorBinding
    sandbox_id: str
    volume_id: str
    state: str
    configuration: dict
    runtime_digest: str
    caller_uid: int
    process_uid: int
    limits_enforced: bool
    saved: bool
    save_nonce: UUID | None = None
    save_revision: int | None = None


class DenyAuthorizer:
    """既定の認可器。Firebase/DBへ接続せず、本人申告だけでは操作させない。"""

    def allowed(self, actor, binding, kind):
        """現在所属の検証機能が未接続なので常に拒否する。"""
        return False


class Adapter:
    """gVisor専用の独立参照アダプター。実行は既定拒否、模擬成功もconnectable=false。"""

    def __init__(self, store, authorizer=None, executor=None, clock_ms=None):
        """一時台帳とモックだけを注入する。実Docker/runsc接続先を指定する引数はない。"""
        self.store = store
        self.authorizer = authorizer if authorizer is not None else DenyAuthorizer()
        self.executor = executor if executor is not None else DisabledExecutor()
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1000000)

    def authorize(self, actor, binding, kind):
        """認証済みactor・現在Team所属を要求する。既存Firebaseを置換するものではない。"""
        if (type(actor) is not int or not isinstance(binding, EditorBinding) or actor != binding.user_id
                or self.authorizer.allowed(actor, binding, kind) is not True):
            raise Denied()

    def checked(self, data):
        """台帳内の型/所有者/資源/操作を照合し、不整合を空き枠として扱わない。"""
        if type(data["records"]) is not dict or type(data["operations"]) is not dict:
            raise Denied()
        cpu = memory = disk = active = 0
        owners = set()
        for key, row in data["records"].items():
            if set(row) != {"binding", "profile", "plan", "state", "generation", "active_op", "caller_uid"}:
                raise Denied()
            binding = EditorBinding(**row["binding"])
            profile = Profile(**row["profile"])
            if (key != identities(binding)[0] or row["state"] not in ("STARTING", "RUNNING", "STOPPING", "STOPPED", "QUARANTINED")
                    or type(row["generation"]) is not int or row["generation"] <= 0
                    or type(row["caller_uid"]) is not int or row["caller_uid"] <= 0 or row["caller_uid"] in owners):
                raise Denied()
            owners.add(row["caller_uid"])
            validate_plan(row["plan"], binding, profile, row["plan"]["image_digest"])
            disk += profile.root_gib + profile.data_gib
            if row["state"] != "STOPPED":
                active += 1
                cpu += profile.cpu
                memory += profile.memory_mib
            if row["state"] in ("STARTING", "STOPPING"):
                op = data["operations"].get(row["active_op"])
                if (not op or op["status"] != "ACTIVE" or op["ticket"]["generation"] != row["generation"]
                        or op["ticket"]["binding"] != row["binding"]
                        or op["ticket"]["kind"] != ("START" if row["state"] == "STARTING" else "STOP")):
                    raise Denied()
            elif row["active_op"] is not None:
                raise Denied()
        budget = self.store.budget
        if active > budget.max_running or cpu > budget.cpu or memory > budget.memory_mib or disk > budget.disk_gib:
            raise Denied()
        return cpu, memory, disk, active

    @staticmethod
    def result(data, operation_id):
        """操作履歴と現在状態を区別し、外部公開URL・VMRoute・認証情報を返さない。"""
        op = data["operations"][str(operation_id)]
        key = identities(EditorBinding(**op["ticket"]["binding"]))[0]
        return {"operation": deepcopy(op), "environment": deepcopy(data["records"][key]),
                "execution": "MOCK_ONLY", "real_runsc": "NOT_RUN", "connectable": False, "launch_enabled": False}

    @safe
    def prepare(self, actor, binding, profile, operation_id, capabilities, save=None):
        """安全設定/短期証跡と資源を検査し、同一トランザクションで操作意図を先に保存する。"""
        kind = "STOP" if save is not None else "START"
        self.authorize(actor, binding, kind)
        if type(operation_id) is not UUID or not isinstance(capabilities, Capabilities) or not isinstance(profile, Profile):
            raise Denied()
        now = self.clock_ms()
        capabilities.validate(binding, now)
        plan = plan_for(binding, profile, capabilities.image_digest)
        policy_hash = validate_plan(plan, binding, profile, capabilities.image_digest)
        key, _ = identities(binding)
        with self.store.transaction(now) as data:
            cpu, memory, disk, active = self.checked(data)
            row = data["records"].get(key)
            op = data["operations"].get(str(operation_id))
            if op:
                ticket = op["ticket"]
                if (ticket["binding"] != binding.model_dump() or ticket["kind"] != kind or ticket["policy_hash"] != policy_hash
                        or ticket["runtime_digest"] != capabilities.runtime_digest
                        or (kind == "STOP" and (ticket["save_nonce"] != str(save.nonce) or ticket["save_revision"] != save.revision))):
                    raise Denied()
                return None, self.result(data, operation_id)
            if row and (row["binding"] != binding.model_dump() or row["profile"] != asdict(profile)
                        or row["plan"] != plan or row["caller_uid"] != capabilities.caller_uid):
                raise Denied()
            if any(r["caller_uid"] == capabilities.caller_uid and k != key for k, r in data["records"].items()):
                raise Denied()  # 別環境へ同じhost principalを割り当てない。UIDの実割当ては未実装。
            if kind == "START":
                added = 0 if row else profile.root_gib + profile.data_gib
                budget = self.store.budget
                if (row and row["state"] != "STOPPED") or cpu + profile.cpu > budget.cpu or memory + profile.memory_mib > budget.memory_mib or active + 1 > budget.max_running or disk + added > min(budget.disk_gib, capabilities.storage_limit_gib):
                    raise Denied()
            else:
                if not row or row["state"] != "RUNNING" or not isinstance(save, SaveProof):
                    raise Denied()
                save.validate(binding, row["generation"], now)
                if any(o["ticket"]["save_nonce"] == str(save.nonce) for o in data["operations"].values()):
                    raise Denied()
            generation = row["generation"] + 1 if row else 1
            ticket = Ticket(operation_id, binding, kind, generation, now + 30000, policy_hash, capabilities.runtime_digest,
                            save.nonce if save else None, save.revision if save else None)
            data["records"][key] = {"binding": binding.model_dump(), "profile": asdict(profile), "plan": plan,
                                    "state": "STARTING" if kind == "START" else "STOPPING", "generation": generation,
                                    "active_op": str(operation_id), "caller_uid": capabilities.caller_uid}
            data["operations"][str(operation_id)] = {"ticket": ticket.serialized(), "status": "ACTIVE"}
            self.checked(data)
            return ticket, self.result(data, operation_id)

    def active(self, data, ticket):
        """設定・所有者・世代・leaseが保存済み意図と完全一致するときだけ完了/送信可能。"""
        self.checked(data)
        if not isinstance(ticket, Ticket):
            raise Denied()
        row = data["records"].get(identities(ticket.binding)[0])
        op = data["operations"].get(str(ticket.operation_id))
        if (not row or not op or encode(op["ticket"]) != encode(ticket.serialized()) or op["status"] != "ACTIVE"
                or row["active_op"] != str(ticket.operation_id) or row["generation"] != ticket.generation):
            raise Denied()
        return row, op

    @staticmethod
    def quarantine(row, op):
        """失敗/不明を未作成と推定せず隔離する。CPU/RAM/保存枠は維持する。"""
        row["state"], row["active_op"], op["status"] = "QUARANTINED", None, "UNKNOWN"

    @safe
    def uncertain(self, actor, ticket):
        """応答消失・検証失敗時に保全する。停止/削除/再作成による帳尻合わせはしない。"""
        self.authorize(actor, ticket.binding, ticket.kind)
        with self.store.transaction(self.clock_ms()) as data:
            row, op = self.active(data, ticket)
            self.quarantine(row, op)
            return self.result(data, ticket.operation_id)

    @safe
    def dispatch(self, actor, ticket):
        """意図確定後だけ注入モックを呼ぶ。実executorは未実装で既定は必ず拒否する。"""
        self.authorize(actor, ticket.binding, ticket.kind)
        with self.store.transaction(self.clock_ms()) as data:
            row, _ = self.active(data, ticket)
            if self.clock_ms() >= ticket.lease_ms:
                raise Denied()
            plan = deepcopy(row["plan"])
        try:
            observation = self.executor.execute(ticket, plan)
            return self.finish(actor, ticket, observation)
        except Exception:
            # 外部で成功した可能性もあるため、再実行や予約解放をせず隔離する。
            return self.uncertain(actor, ticket)

    @safe
    def finish(self, actor, ticket, observation):
        """起動inspect・保存/停止の証跡が一致した場合だけ成功とする。MOCK結果は接続許可にしない。"""
        self.authorize(actor, ticket.binding, ticket.kind)
        now = self.clock_ms()
        with self.store.transaction(now) as data:
            row, op = self.active(data, ticket)
            valid = isinstance(observation, MockObservation) and now < ticket.lease_ms
            if valid:
                sandbox, volume = identities(ticket.binding)
                valid = (observation.ticket == ticket and observation.binding == ticket.binding
                         and observation.sandbox_id == sandbox and observation.volume_id == volume
                         and observation.runtime_digest == ticket.runtime_digest
                         and observation.caller_uid == row["caller_uid"] and type(observation.caller_uid) is int
                         and observation.process_uid == 1000 and type(observation.process_uid) is int
                         and observation.limits_enforced is True and encode(observation.configuration) == encode(row["plan"]))
                if ticket.kind == "START":
                    valid = valid and observation.state == "RUNNING" and observation.save_nonce is None and observation.save_revision is None
                else:
                    valid = (valid and observation.state == "STOPPED" and observation.saved is True
                             and type(observation.save_nonce) is UUID and observation.save_nonce == ticket.save_nonce
                             and type(observation.save_revision) is int and observation.save_revision == ticket.save_revision)
            if valid:
                row["state"], row["active_op"], op["status"] = observation.state, None, "SUCCEEDED"
            else:
                self.quarantine(row, op)
            return self.result(data, ticket.operation_id)

    @safe
    def state(self, actor, binding):
        """現在状態を読み、期限切れ進行中操作を隔離する。VMRouteは作成しない。"""
        self.authorize(actor, binding, "STATE")
        now = self.clock_ms()
        with self.store.transaction(now) as data:
            self.checked(data)
            row = data["records"].get(identities(binding)[0])
            if row is None:
                return None
            if row["binding"] != binding.model_dump():
                raise Denied()
            if row["active_op"]:
                op = data["operations"][row["active_op"]]
                if now >= op["ticket"]["lease_ms"]:
                    self.quarantine(row, op)
            return {"environment": deepcopy(row), "connectable": False, "real_runsc": "NOT_RUN"}

    def request(self, actor, binding, profile, operation_id, capabilities, save=None):
        """準備済み新規操作だけをdispatchし、同じIDの再送は保存済み履歴を返す。"""
        ticket, result = self.prepare(actor, binding, profile, operation_id, capabilities, save)
        return result if ticket is None else self.dispatch(actor, ticket)
