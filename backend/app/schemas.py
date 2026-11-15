from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional
from datetime import datetime

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
    name: str
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Project name must not be empty.")
        return value


class ProjectResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    team_id: int
    created_by: int
    created_at: datetime

    class Config:
        from_attributes = True


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
