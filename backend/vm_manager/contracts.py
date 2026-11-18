"""実基盤を操作しない管理契約。実アダプターはなく、既定は常に拒否する。

既存EditorBinding/WorkerCommandは変更せず、内部のfencing情報を別の封筒へ束縛する。
署名・mTLS・Firebase認証の代わりではない。現在の呼出し元はテストだけ。
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, NAMESPACE_URL, uuid5

from app.editor_contracts import EditorBinding, WorkerCommand

BUSY_STATES = ("PREPARING", "STARTING", "RUNNING", "STOPPING", "QUARANTINED")
STATES = ("ABSENT", "STOPPED", *BUSY_STATES)


class ManagerError(Exception):
    """固定コードだけを公開する。DB・パス・アダプターの生エラーは保持しない。"""


class Forbidden(ManagerError): pass
class Conflict(ManagerError): pass
class CapacityExceeded(ManagerError): pass
class RegistryUnavailable(ManagerError): pass
class ClockRegression(ManagerError): pass
class DriverUnavailable(ManagerError): pass
class StaleFence(ManagerError): pass
class OwnershipMismatch(ManagerError): pass


def disk_key(binding: EditorBinding) -> str:
    # VMとdata diskは別ID。Project/Userからサーバー側で固定し、要求から受け取らない。
    return str(uuid5(NAMESPACE_URL, f"team-progress/editor-data/v1/{binding.project_id}/{binding.user_id}"))


@dataclass(frozen=True)
class Fence:
    command: WorkerCommand
    vm_id: str
    disk_id: str
    generation: int
    lease_until_ms: int
    require_vm: bool = False
    require_disk: bool = False

    def __post_init__(self):
        if (self.vm_id != str(self.command.binding.environment_key) or self.disk_id != disk_key(self.command.binding)
                or type(self.generation) is not int or self.generation <= 0
                or type(self.lease_until_ms) is not int or self.lease_until_ms <= 0
                or type(self.require_vm) is not bool or type(self.require_disk) is not bool):
            raise Conflict("invalid_fence")


@dataclass(frozen=True)
class Observation:
    operation_id: UUID
    generation: int
    vm_id: str
    disk_id: str
    state: str
    vm_owner: EditorBinding | None
    disk_owner: EditorBinding | None

    def verify(self, fence: Fence):
        if (self.operation_id != fence.command.operation_id or type(self.generation) is not int or self.generation != fence.generation
                or self.vm_id != fence.vm_id or self.disk_id != fence.disk_id
                or self.state not in ("ABSENT", "STOPPED", "RUNNING")):
            raise Conflict("invalid_observation")
        binding = fence.command.binding
        if self.state == "ABSENT":
            if self.vm_owner is not None or self.disk_owner not in (None, binding):
                raise OwnershipMismatch("owner_mismatch")
        elif self.vm_owner != binding or self.disk_owner != binding:
            raise OwnershipMismatch("owner_mismatch")


class Authorizer(Protocol):
    def allowed(self, actor_user_id: int, binding: EditorBinding, operation: str) -> bool: ...


class FencedDriver(Protocol):
    """境界側でも期限・世代・所有者・冪等性を検査してから副作用を実行する契約。

    GET_STATEはfence-and-observeで、古い世代を受理しない記録を先に確定する。
    実libvirtへの接続や、この保証を実環境で実現するアダプターは未実装。
    不明な未完了jobをSTOPPEDと返して枠を解放してはいけない。
    """
    def execute(self, fence: Fence) -> Observation: ...


class DisabledDriver:
    def execute(self, fence: Fence) -> Observation:
        # 環境変数やフラグで実接続へ切り替える分岐はない。
        raise DriverUnavailable("real_driver_disabled")
