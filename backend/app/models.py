
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
    text,
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
    # 判定は提出情報と別に保持する。未判定の依頼には子レコードがないため、
    # 既存データを書き換えずにPENDINGとして扱える。
    # selectinで一覧分の判定をまとめて取得し、FOR UPDATEの対象行へ外部結合しない。
    review = relationship("ChangeRequestReview", back_populates="change_request", uselist=False, lazy="selectin")

    @property
    def submitter_name(self):
        return self.submitter.name

    @property
    def review_decision(self):
        """提出依頼のOPEN/CLOSEDとは独立した、レビューの判定を返す。"""
        return self.review.decision if self.review else "PENDING"

    @property
    def reviewer_id(self):
        """未判定なら判定者はいない。提出者IDを代用しない。"""
        return self.review.reviewer_id if self.review else None

    @property
    def reviewer_name(self):
        """判定者名は認証済みUserの記録から参照し、リクエスト本文から保存しない。"""
        return self.review.reviewer.name if self.review else None

    @property
    def reviewed_at(self):
        """判定が確定したUTC日時。既存の提出日時を変更せずに返す。"""
        return self.review.reviewed_at if self.review else None

    @property
    def review_reason(self):
        """差し戻し理由。未判定・承認の場合はnullを返す。"""
        return self.review.reason if self.review else None


class ChangeRequestReview(Base):
    """一つの依頼に一つの確定判定を記録する。PENDINGはレコードなしで表現する。"""
    __tablename__ = "change_request_reviews"
    __table_args__ = (
        CheckConstraint("decision IN ('APPROVED', 'CHANGES_REQUESTED')", name="ck_request_review_decision"),
        # CHECKはNULLを許容する性質があるため、差し戻し側ではIS NOT NULLも明示する。
        CheckConstraint(
            "(decision = 'APPROVED' AND reason IS NULL) OR "
            "(decision = 'CHANGES_REQUESTED' AND reason IS NOT NULL "
            "AND length(trim(reason)) BETWEEN 1 AND 2000)",
            name="ck_request_review_reason",
        ),
        # 開発用Composeはソースをbind mountして自動reloadするため、
        # モデルを追加しただけで実DBにDDLを流さないよう明示的な移行を必須にする。
        {"info": {"requires_explicit_migration": True}},
    )

    # 依頼IDを主キーにすることで、アプリの行ロックを使わない別経路からの
    # 同時INSERTがあっても、DBが二件目の判定を受け付けない。
    change_request_id = Column(Integer, ForeignKey("change_requests.id", ondelete="RESTRICT"), primary_key=True)
    decision = Column(String(20), nullable=False)
    reviewer_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    reviewed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    reason = Column(String(2000), nullable=True)

    change_request = relationship("ChangeRequest", back_populates="review")
    reviewer = relationship("User", lazy="selectin")


class ChangeRequestIntegration(Base):
    """GitとDBの二段階処理を記録する。PREPAREDはGit更新済みの可能性もある。

    既存の提出状態やレビュー判定とは独立させる。COMPLETEDになっても依頼を
    CLOSEDへ変更しない。履歴を消さず、再送には同じoperation_idの結果を返す。
    """
    __tablename__ = "change_request_integrations"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_integration_operation"),
        CheckConstraint("status IN ('PREPARED', 'COMPLETED')", name="ck_integration_status"),
        CheckConstraint(
            "(status = 'PREPARED' AND integrated_at IS NULL) OR "
            "(status = 'COMPLETED' AND integrated_at IS NOT NULL AND integrated_at >= prepared_at)",
            name="ck_integration_time",
        ),
        CheckConstraint("length(operation_id) = 36", name="ck_integration_operation"),
        CheckConstraint(
            "length(main_after) = 40 AND (main_before IS NULL OR "
            "(length(main_before) = 40 AND main_before <> main_after))", name="ck_integration_commits",
        ),
        # 未確定の処理があるProjectでは次のGit更新を始めない。
        # SQLite側も同じ部分一意索引とし、テストと本番の制約の意味を揃える。
        Index("uq_integration_pending_project", "project_id", unique=True,
              postgresql_where=text("status = 'PREPARED'"),
              sqlite_where=text("status = 'PREPARED'")),
        {"info": {"requires_explicit_migration": True}},
    )

    change_request_id = Column(Integer, ForeignKey("change_requests.id", ondelete="RESTRICT"), primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    operation_id = Column(String(36), nullable=False)
    integrated_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    status = Column(String(16), nullable=False)
    prepared_at = Column(DateTime, nullable=False)
    integrated_at = Column(DateTime, nullable=True)
    # NULLは、仮想baseではなく「実際のmain参照がまだ存在しなかった」を表す。
    main_before = Column(String(40), nullable=True)
    main_after = Column(String(40), nullable=False)


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

