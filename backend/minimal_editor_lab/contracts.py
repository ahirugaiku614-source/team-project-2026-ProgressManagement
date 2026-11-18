"""最小構成と停止判断の契約。測定値ではなく、試験用の予約・観測データを定義する。"""

from dataclasses import dataclass
from uuid import UUID

from app.editor_contracts import EditorBinding
from vm_manager.contracts import Fence, Observation


class LabDenied(Exception):
    """拒否理由に入力値/内部パス/資格情報を含めない、試験モデル共通の固定例外。"""

    def __init__(self):
        super().__init__("minimal_editor_lab_denied")


def positive(value, maximum=2**31):
    """bool・負数・巨大値を予約量/時刻として受理しない。"""
    if type(value) is not int or not 0 < value <= maximum:
        raise LabDenied()


@dataclass(frozen=True)
class Profile:
    """環境1組のCPU/RAMと停止後も予約するroot/data容量。実行性能の合格証明ではない。"""

    cpu: int
    memory_mib: int
    root_gib: int = 16
    data_gib: int = 20

    def __post_init__(self):
        """今回比較する4構成だけを認め、保存量を小さくして無料判定するのを防ぐ。"""
        for value in (self.cpu, self.memory_mib, self.root_gib, self.data_gib):
            positive(value)
        if (self.cpu, self.memory_mib) not in ((1, 1024), (1, 2048), (2, 2048), (2, 4096)) or (self.root_gib, self.data_gib) != (16, 20):
            raise LabDenied()


PROFILES = (Profile(1, 1024), Profile(1, 2048), Profile(2, 2048), Profile(2, 4096))


@dataclass(frozen=True)
class Budget:
    """ホスト/制御系/安全余裕を差し引いた予約可能量。クラウド総量そのものではない。"""

    cpu: int
    memory_mib: int
    disk_gib: int
    max_running: int = 4

    def __post_init__(self):
        """新規試験台帳の上限を検査する。既存全体4枠を引き上げる設定は持たない。"""
        for value in (self.cpu, self.memory_mib, self.disk_gib, self.max_running):
            positive(value)
        if self.max_running > 4:
            raise LabDenied()


@dataclass(frozen=True)
class StorageEvidence:
    """保存サービスが証明すべき短期の総予約可能容量。空きbyteをそのまま流用しない。

    limit_gibは既存の未使用予約も考慮した総commit上限。試験ではモックが供給する。
    実FSの使用量・quota・inode・thin metadata・故障を検査するサービスは未実装。
    """

    limit_gib: int
    sampled_ms: int
    healthy: bool


@dataclass(frozen=True)
class IdleEvidence:
    """停止前の全クライアント保存確認とジョブ静止の証跡。ブラウザの自己申告だけで作らない。

    quiescence_idは新job/接続受付を凍結してflushを確認した境界の一回限りの識別子。
    欠測を0/falseとみなさない。集約器・凍結・実ジョブ検出は実基盤ではまだ未実装。
    """

    binding: EditorBinding
    generation: int
    revision: int
    quiescence_id: UUID
    sampled_ms: int
    last_activity_ms: int
    jobs: int | None
    clients: int | None
    dirty: bool | None
    saving: bool | None
    saved_ack: bool | None

    def validate(self, binding, generation, now):
        """10分の無活動と5秒以内の確実な保存/ジョブ静止を要求する。不明なら停止禁止。"""
        if (self.binding != binding or type(self.generation) is not int or self.generation != generation
                or type(self.revision) is not int or self.revision <= 0 or type(self.quiescence_id) is not UUID
                or type(self.sampled_ms) is not int or not 0 <= now - self.sampled_ms <= 5000
                or type(self.last_activity_ms) is not int or not 0 < self.last_activity_ms <= self.sampled_ms
                or now - self.last_activity_ms < 600000
                or type(self.jobs) is not int or self.jobs != 0 or type(self.clients) is not int or self.clients != 0
                or self.dirty is not False or self.saving is not False or self.saved_ack is not True):
            raise LabDenied()


@dataclass(frozen=True)
class StopPlan:
    """既存Fenceと静止証跡を束縛する停止意図。これを作っても実停止は行わない。"""

    fence: Fence
    evidence: IdleEvidence


@dataclass(frozen=True)
class StopAck:
    """静止境界が返す保存/停止確認。実装時は認証済み境界が生成し、要求者に作らせない。"""

    observation: Observation
    quiescence_id: UUID
    revision: int
    saved_ack: bool


def require_real_runtime():
    """実VM/コンテナ作成を常に拒否する。環境変数で有効化する分岐はない。"""
    raise LabDenied()
