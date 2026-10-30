
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    ForeignKey,
    DateTime,
    Enum,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from app.db.database import Base


# タスクのステータス定義
class TaskStatus(str, enum.Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


# ユーザーテーブル
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    firebase_uid = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # 自分が所有するタスク
    tasks = relationship(
        "Task",
        foreign_keys="Task.user_id",
        back_populates="owner",
    )

    # 所属しているチーム
    team_memberships = relationship(
        "TeamMember",
        back_populates="user",
    )


# チームテーブル
class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    invite_code = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # チームに所属するメンバー
    members = relationship(
        "TeamMember",
        back_populates="team",
        cascade="all, delete-orphan",
    )

    # チームに紐づくタスク
    tasks = relationship(
        "Task",
        back_populates="team",
    )


# チームメンバーテーブル
class TeamMember(Base):
    __tablename__ = "team_members"

    __table_args__ = (
        UniqueConstraint(
            "team_id",
            "user_id",
            name="uq_team_member",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(
        Integer,
        ForeignKey("teams.id"),
        nullable=False,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
    )
    role = Column(String, default="member", nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="members")
    user = relationship("User", back_populates="team_memberships")


# タスクテーブル
class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(
        Enum(TaskStatus),
        default=TaskStatus.TODO,
        nullable=False,
    )

    # タスクの作成者
    user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
    )

    # チームと担当者（任意）
    team_id = Column(
        Integer,
        ForeignKey("teams.id"),
        nullable=True,
    )
    assignee_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=True,
    )

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    owner = relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="tasks",
    )
    assignee = relationship(
        "User",
        foreign_keys=[assignee_id],
    )
    team = relationship("Team", back_populates="tasks")
