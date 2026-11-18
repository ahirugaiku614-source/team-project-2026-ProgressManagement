"""既存の永続VMモックを使う試験補助。実VM・disk・プロセスを作らない。"""

from uuid import UUID, uuid4

from app.editor_contracts import EditorBinding
from minimal_editor_lab.contracts import IdleEvidence, LabDenied, Profile, StopAck, StorageEvidence
from minimal_editor_lab.ledger import Ledger
from minimal_editor_lab.model import ResourceModel
from vm_manager_fakes import Clock, Members, PersistentFakeDriver


class FrozenBoundary:
    """凍結した活動revisionを最終停止境界でも比較するモック。host/guest観測は行わない。"""

    def __init__(self, driver, clock):
        """永続モックと仮想時計を受け取り、凍結後の変更を試験で注入できるようにする。"""
        self.driver, self.clock = driver, clock
        self.revision = 1
        self.jobs = 0
        self.dirty = False

    def stop(self, plan):
        """証跡が今も有効な場合だけ停止行を更新する。job開始/dirty/期限切れは副作用前に拒否。"""
        evidence = plan.evidence
        evidence.validate(plan.fence.command.binding, plan.fence.generation - 1, self.clock())
        if evidence.revision != self.revision or self.jobs != 0 or self.dirty is not False:
            raise LabDenied()
        observation = self.driver.execute(plan.fence)
        return StopAck(observation, evidence.quiescence_id, self.revision, True)


def evidence_for(binding, generation, clock):
    """10分以上静止し、全clientが保存済みの合成証跡を生成する。実測ではない。"""
    return IdleEvidence(binding, generation, 1, uuid4(), clock(), clock() - 600000, 0, 0, False, False, True)


def concurrent_reserve(path, user, operation_id, barrier, queue):
    """別プロセスから同じ試験台帳へ予約し、メモリカウンターに依存しないことを確認する。"""
    clock = Clock(1000000)
    binding = EditorBinding(team_id=1, project_id=1, user_id=user)
    model = ResourceModel(Ledger(path), Members([binding]), clock)
    try:
        barrier.wait(timeout=20)
        model.prepare_start(user, binding, Profile(1, 1024), UUID(operation_id), StorageEvidence(1000, clock(), True))
        queue.put("reserved")
    except Exception as error:
        queue.put(str(error))
