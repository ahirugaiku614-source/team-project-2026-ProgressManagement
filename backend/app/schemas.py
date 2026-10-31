from pydantic import BaseModel, EmailStr
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
