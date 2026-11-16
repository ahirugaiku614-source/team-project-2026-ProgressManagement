
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    ForeignKey,
    DateTime,
    Enum,
    UniqueConstraint,
    CheckConstraint,
    Index,
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

    projects = relationship("Project", back_populates="team")


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    team = relationship("Team", back_populates="projects")
    creator = relationship("User", foreign_keys=[created_by])
    change_requests = relationship("ChangeRequest", back_populates="project")


class ChangeRequest(Base):
    """提出後に変更しないコミットと、再試行用の提出キーを保存する。"""
    __tablename__ = "change_requests"
    __table_args__ = (
        UniqueConstraint("project_id", "submitted_by", "request_key", name="uq_change_request_key"),
        UniqueConstraint("git_ref", name="uq_change_request_git_ref"),
        CheckConstraint("state IN ('OPEN', 'CLOSED')", name="ck_change_request_state"),
        CheckConstraint("length(title) BETWEEN 1 AND 200", name="ck_change_request_title"),
        CheckConstraint("description IS NULL OR length(description) <= 10000", name="ck_change_request_description"),
        CheckConstraint("length(snapshot_commit) = 40 AND length(base_commit) = 40", name="ck_change_request_commits"),
        Index("ix_change_requests_project_created", "project_id", "created_at", "id"),
    )

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    submitted_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    request_key = Column(String(36), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    state = Column(String(16), nullable=False, default="OPEN", server_default="OPEN")
    snapshot_commit = Column(String(40), nullable=False)
    base_commit = Column(String(40), nullable=False)
    git_ref = Column(String(128), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="change_requests")
    submitter = relationship("User")

    @property
    def submitter_name(self):
        return self.submitter.name


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


# コードファイルテーブル
class CodeFile(Base):
    __tablename__ = "code_files"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "name",
            name="uq_code_file_user_name",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    # ファイル所有者
    user_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
    )

    # ファイル情報
    name = Column(String, nullable=False)
    language = Column(String, nullable=False)
    content = Column(Text, nullable=False, default="")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

