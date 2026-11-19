from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Literal, Optional
from datetime import datetime
from uuid import UUID
from pydantic import ConfigDict

class UserBase(BaseModel):
    email: EmailStr
    name: Optional[str] = None

class UserCreate(UserBase):
    firebase_uid: str

class UserResponse(UserBase):
    id: int
    firebase_uid: str
    created_at: datetime

    class Config:
        from_attributes = True

class TaskBase(BaseModel):
    title: str
    description: Optional[str] = None
    status: str = "todo"  # todo, in_progress, done

class TaskCreate(TaskBase):
    pass

class TaskResponse(TaskBase):
    id: int
    user_id: int
    created_at: datetime

    class Config:
        from_attributes = True

class TeamCreate(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class ProjectResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    team_id: int
    created_by: int
    created_at: datetime

    class Config:
        from_attributes = True


class ProjectRepositoryState(BaseModel):
    project_id: int
    repository_state: Literal["missing", "ready", "incomplete"]
    default_branch: str = "main"


class ProjectWorkspaceState(ProjectRepositoryState):
    user_id: int
    workspace_state: Literal["missing", "ready", "incomplete"]


class ChangeRequestCreate(BaseModel):
    # 他人のユーザーID・パス・Gitコマンドなどの余計な入力も受け付けない。
    model_config = ConfigDict(extra="forbid")
    request_key: UUID
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=10000)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("title", "description")
    @classmethod
    def validate_utf8(cls, value):
        if value is not None:
            value.encode("utf-8")
            if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in value):
                raise ValueError("Control characters are not allowed.")
        return value


class ChangeRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    submitted_by: int
    submitter_name: Optional[str] = None
    request_key: UUID
    title: str
    description: Optional[str]
    state: Literal["OPEN", "CLOSED"]
    snapshot_commit: str
    base_commit: str
    git_ref: str
    created_at: datetime
    updated_at: datetime
    # 既存のキー・意味は維持し、この5項目だけを追加する。
    # 古い依頼は判定レコードがないため、PENDINGとnullが返る。
    review_decision: Literal["PENDING", "APPROVED", "CHANGES_REQUESTED"] = "PENDING"
    reviewer_id: Optional[int] = None
    reviewer_name: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_reason: Optional[str] = None


class ChangeRequestApprove(BaseModel):
    """承認にユーザーIDやGit情報は不要。指定されても信用せず422で拒否する。"""
    model_config = ConfigDict(extra="forbid")


class ChangeRequestChanges(BaseModel):
    """差し戻しは理由だけを受け付け、判定者・判定日時はサーバーが決める。"""
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value):
        # 先に空白を除くことで、見た目が空の理由や空白だけの2000文字を受け付けない。
        return value.strip() if isinstance(value, str) else value

    @field_validator("reason")
    @classmethod
    def validate_reason_text(cls, value):
        # PostgreSQLで保存できないNUL、不正Unicode等をDB操作前に弾く。
        # 改行・タブを含む複数行の理由は許可する。
        value.encode("utf-8")
        if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in value):
            raise ValueError("Control characters are not allowed.")
        return value


class ChangeRequestIntegrate(BaseModel):
    """統合先・ユーザー・操作IDはサーバーで確定する。本文での指定を拒否する。"""
    model_config = ConfigDict(extra="forbid")


class ChangeRequestIntegrationResponse(BaseModel):
    """PREPAREDを未統合と断定しない。再送でGitを照合してから確定する。"""
    model_config = ConfigDict(from_attributes=True)
    change_request_id: int
    project_id: int
    operation_id: UUID
    integrated_by: int
    status: Literal["PREPARED", "COMPLETED"]
    prepared_at: datetime
    integrated_at: Optional[datetime]
    main_before: Optional[str]
    main_after: str


class ChangedFileResponse(BaseModel):
    path: str
    change: Literal["A", "M", "D"]
    binary: bool
    mode_changed: bool
    patch: Optional[str]
    truncated: bool


class ChangeRequestDiffResponse(BaseModel):
    base_commit: str
    snapshot_commit: str
    files: list[ChangedFileResponse]
    truncated: bool


# チーム情報の返却データ
class TeamResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    invite_code: str
    created_at: datetime

    class Config:
        from_attributes = True


# 招待コードによるチーム参加時の入力データ
class TeamJoinRequest(BaseModel):
    invite_code: str

# チームメンバー情報の返却データ
class TeamMemberResponse(BaseModel):
    user_id: int
    name: Optional[str] = None
    email: EmailStr
    role: str
    joined_at: datetime

    class Config:
        from_attributes = True

# チームタスク作成時の入力データ
class TeamTaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    status: str = "TODO"
    assignee_id: Optional[int] = None


# チームタスクの返却データ
class TeamTaskResponse(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    status: str
    user_id: int
    team_id: int
    assignee_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


# コードファイル作成・更新用
class CodeFileCreate(BaseModel):
    name: str
    language: str
    content: str = ""


# コードファイル更新用
class CodeFileUpdate(BaseModel):
    name: str
    language: str
    content: str


# コードファイル取得用
class CodeFileResponse(BaseModel):
    id: int
    user_id: int
    name: str
    language: str
    content: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# 短時間のPython実行専用の上限。既存の個人エディタ用Profileは変更しない。
EXECUTION_CODE_MAX_BYTES = 64 * 1024
EXECUTION_OUTPUT_MAX_BYTES = 64 * 1024


def validate_execution_code(value: str) -> str:
    """実行せずにUTF-8 byte数・空白だけ・NULを検査し、インデントを保持して返す。"""
    if not value.strip() or "\x00" in value:
        raise ValueError("Invalid Python source")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("Invalid Python source") from None
    if size > EXECUTION_CODE_MAX_BYTES:
        raise ValueError("Python source is too large")
    return value


class ExecutionCreate(BaseModel):
    """POST /executeの本文。本人IDやimage/mount/コマンド等の追加指定を拒否する。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    team_id: int = Field(gt=0, le=2**31 - 1)
    project_id: int = Field(gt=0, le=2**31 - 1)
    language: Literal["python"] = "python"
    code: str = Field(min_length=1, max_length=EXECUTION_CODE_MAX_BYTES, repr=False)

    @field_validator("code")
    @classmethod
    def check_source(cls, value):
        """構文の正しさは判定しない。SyntaxErrorも将来のworker実行結果として扱う。"""
        return validate_execution_code(value)


class ExecutionResponse(BaseModel):
    """workerから確認できた実行結果。API側が終了コードや実行時間を推測しない。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    status: Literal["completed", "error", "timeout"]
    stdout: str = Field(max_length=EXECUTION_OUTPUT_MAX_BYTES, repr=False)
    stderr: str = Field(max_length=EXECUTION_OUTPUT_MAX_BYTES, repr=False)
    exit_code: int | None = Field(ge=-(2**31), le=2**31 - 1)
    execution_time_ms: int = Field(ge=0, le=2**31 - 1)

    @field_validator("stdout", "stderr")
    @classmethod
    def check_output(cls, value):
        """モック結果もUTF-8 byte上限を検査する。実workerのストリーム制限は未実装。"""
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise ValueError("Invalid execution output") from None
        if size > EXECUTION_OUTPUT_MAX_BYTES:
            raise ValueError("Execution output is too large")
        return value

    @model_validator(mode="after")
    def check_exit_status(self):
        """成功・異常・期限切れの矛盾を拒否する。期限切れはexit_code未確定とする。"""
        if ((self.status == "completed" and self.exit_code != 0)
                or (self.status == "error" and self.exit_code == 0)
                or (self.status == "timeout" and self.exit_code is not None)):
            raise ValueError("Inconsistent execution result")
        return self
