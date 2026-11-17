"""将来の専用ワーカーと共有するデータ形式。通信・起動・認証許可は実行しない。

ここで検証できるのは型と対応関係だけ。DTOを作れたことを実行許可にしてはいけない。
本番のmTLS・署名付き短期許可・nonceの一回消費・現在所属の再確認は別途必要。
任意のDocker設定や保存パス、到達先URLを受け付ける形式にはしない。
"""

from typing import Literal, Protocol
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field


class EditorBinding(BaseModel):
    """認証済みUserとDBのProjectからだけ作成する、編集環境の所有関係。"""
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    team_id: int = Field(gt=0)
    project_id: int = Field(gt=0)
    user_id: int = Field(gt=0)

    @property
    def environment_key(self) -> UUID:
        """重複起動検出用の安定キー。推測困難性にも認可にも使用しない。

        Projectの所属Teamが変わっても保存環境のキーは変えない。
        ただしコマンド・応答ではteam_idを含むBinding全体の一致が必要となる。
        """
        return uuid5(NAMESPACE_URL, f"team-progress/editor/v1/{self.project_id}/{self.user_id}")


class WorkerCommand(BaseModel):
    """許可する操作と所有関係だけを定義する。パス・イメージ等はワーカーの固定設定。

    operation_idは将来の冪等性・監査用。自由なexec、削除、任意URL、認証情報を含めない。
    このモデルだけでは実行許可にならず、現在のアプリは送信自体を禁止する。
    """
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    operation_id: UUID
    operation: Literal["ENSURE_STARTED", "GET_STATE", "STOP"]
    binding: EditorBinding


class WorkerState(BaseModel):
    """将来の応答にもURL・パス・パスワードを含めない。本人環境以外の応答は拒否する。"""
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    operation_id: UUID
    binding: EditorBinding
    state: Literal["STARTING", "RUNNING", "STOPPED", "ERROR"]


def validate_worker_reply(command: WorkerCommand, reply: WorkerState):
    """別User・Project・Team・操作の応答を取り違えないための検査。

    RUNNINGが返る形式を定義しても、起動や隔離を検証済みとはみなさない。
    実機のヘルスチェック・マウント実測・リース世代の検査は基盤実装時に追加する。
    """
    if reply.operation_id != command.operation_id or reply.binding != command.binding:
        raise ValueError("Worker response does not match the requested environment")
    return reply


class EditorRuntimeUnavailable(Exception):
    """実行基盤の準備・検証が完了していない。内部接続情報は含めない。"""


class EditorWorker(Protocol):
    """APIから汎用Docker APIへ接続しないための、将来の限定通信インターフェース。"""
    def execute(self, command: WorkerCommand) -> WorkerState: ...


class DisabledEditorWorker:
    """現段階で唯一のアダプター。環境変数・入力に関係なく一切通信しない。"""
    def execute(self, command: WorkerCommand) -> WorkerState:
        raise EditorRuntimeUnavailable()


def require_editor_runtime():
    """未準備の基盤をフラグ一つで有効化しない、明示的な実装上のゲート。

    起動APIだけでなく停止・セッション発行も同じゲートを通す。
    ワーカー・TLS・保存先統一・失効処理の検証を経た別PRでのみ置換する。
    """
    raise EditorRuntimeUnavailable()
