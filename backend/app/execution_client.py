"""Python実行workerとの限定契約と単一process内の受付制限。通信・実行機能は未接続。"""

from threading import BoundedSemaphore
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.editor_contracts import EditorBinding
from app.schemas import EXECUTION_CODE_MAX_BYTES, ExecutionResponse, validate_execution_code


class WorkerUnavailable(Exception):
    """未接続・不正応答・worker障害を固定例外へ変換し、内部情報を露出させない。"""


class ExecutionBusy(Exception):
    """このクライアントの同時受付4件を超えた。待機列は作らない。"""


class ExecutionPolicy(BaseModel):
    """将来workerが強制する固定資源契約。APIでDocker設定へ変換するものではない。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    cpu_millis: Literal[500] = 500
    memory_bytes: Literal[134217728] = 128 * 1024 * 1024
    timeout_ms: Literal[5000] = 5000
    network: Literal["none"] = "none"
    runtime: Literal["runsc"] = "runsc"


class ExecutionCommand(BaseModel):
    """認可後にAPIが生成するRUN_PYTHON専用要求。任意exec・URL・mountを持たない。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    execution_id: UUID
    owner: EditorBinding
    operation: Literal["RUN_PYTHON"] = "RUN_PYTHON"
    language: Literal["python"] = "python"
    code: str = Field(min_length=1, max_length=EXECUTION_CODE_MAX_BYTES, repr=False)
    policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)

    @field_validator("code")
    @classmethod
    def check_source(cls, value):
        """HTTP以外からのモック呼出しでも、同じ入力検査を適用する。"""
        return validate_execution_code(value)


class ExecutionReply(BaseModel):
    """結果を実行IDと本人・Team・Projectへ束縛し、別実行の結果を混ぜない応答。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    execution_id: UUID
    owner: EditorBinding
    result: ExecutionResponse


class ExecutionWorker(Protocol):
    """限定操作だけの注入境界。第1段階はテストモックだけが正常応答を返す。"""

    def execute(self, command: ExecutionCommand) -> ExecutionReply:
        """将来workerで実行する契約。API内の学生コード実行は禁止。"""
        ...


class DisconnectedWorker:
    """本番アプリの既定worker。環境変数や設定によるDocker有効化経路はない。"""

    def execute(self, command: ExecutionCommand) -> ExecutionReply:
        """外部へ接続せず固定拒否する。Docker CLI/SDK/socketを一切使わない。"""
        raise WorkerUnavailable()


class ExecutionClient:
    """単一インスタンス内の同期呼出しを最大4件に制限するモック接続用クライアント。"""

    def __init__(self, worker: ExecutionWorker | None = None):
        """テスト時だけworkerを注入する。複数process/hostの共有予約台帳は持たない。"""
        self.worker = worker if worker is not None else DisconnectedWorker()
        self._slots = BoundedSemaphore(4)

    def execute(self, command: ExecutionCommand) -> ExecutionResponse:
        """即時受付・呼出し・所有者照合を行い、モック呼出し終了時に必ず受付枠を戻す。"""
        if not self._slots.acquire(blocking=False):
            raise ExecutionBusy()
        try:
            # model_construct等で作られた不正な合成値も、外部境界へ渡す前後に再検査する。
            command = ExecutionCommand.model_validate(command.model_dump())
            reply = self.worker.execute(command)
            if not isinstance(reply, ExecutionReply):
                raise WorkerUnavailable()
            reply = ExecutionReply.model_validate(reply.model_dump())
            if reply.execution_id != command.execution_id or reply.owner != command.owner:
                raise WorkerUnavailable()
            return reply.result
        except ExecutionBusy:
            # 将来のworker共通予約で満枠だった場合も、APIの429へ対応付ける。
            raise
        except Exception:
            # 学生コード・SDK例外・パス等を応答やログへ出さない。
            raise WorkerUnavailable() from None
        finally:
            # これはAPI呼出し枠のみ。実コンテナの停止や分散予約解放を保証しない。
            self._slots.release()


_client = ExecutionClient()


def get_execution_client() -> ExecutionClient:
    """同一processで共有する既定クライアント。テストはFastAPI依存上書きで代替する。"""
    return _client
