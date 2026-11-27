
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


class TaskSubmissionTarget(Base):
    """提出対象だけを明示登録する。既存Taskの列・レスポンスは変更しない。"""
    __tablename__ = "task_submission_targets"
    __table_args__ = (
        UniqueConstraint("project_id", "student_id", "folder_path", name="uq_task_submission_folder"),
        CheckConstraint("length(folder_path) BETWEEN 1 AND 512", name="ck_task_submission_folder_length"),
        {"info": {"requires_explicit_migration": True}},
    )

    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    folder_path = Column(String(512), nullable=False)
    registered_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"))


class WorkBasis(Base):
    """編集開始の不変基準。Task/セッション削除で履歴を失わない。"""
    __tablename__ = 'work_bases'
    __table_args__ = (CheckConstraint("state IN ('ACTIVE','ENDED','SUPERSEDED','REVOKED')", name='ck_work_basis_state'),
                      {'info': {'requires_explicit_migration': True}})
    id = Column(String(36), primary_key=True)
    member_id = Column(Integer, ForeignKey('users.id', ondelete='RESTRICT'), nullable=False)
    team_id = Column(Integer, ForeignKey('teams.id', ondelete='RESTRICT'), nullable=False)
    project_id = Column(Integer, ForeignKey('projects.id', ondelete='RESTRICT'), nullable=False)
    task_id = Column(Integer, nullable=False)
    workspace_id = Column(String(36), nullable=False, unique=True)
    source_generation = Column(Integer, nullable=False)
    folder_path = Column(String(512), nullable=False)
    binding_version = Column(String(64), nullable=False)
    base_commit = Column(String(40), nullable=False)
    policy_version = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False)
    state = Column(String(16), nullable=False)


class WorkCaptureBinding(Base):
    """収集開始前に基準を固定する。過去の収集へ基準を後付けしない。"""
    __tablename__ = 'work_capture_bindings'
    __table_args__ = ({'info': {'requires_explicit_migration': True}},)
    submission_id = Column(String(36),ForeignKey('task_submissions.id',ondelete='RESTRICT'),primary_key=True)
    basis_id = Column(String(36),ForeignKey('work_bases.id',ondelete='RESTRICT'),nullable=False)
    workspace_id = Column(String(36),nullable=False)
    source_generation = Column(Integer,nullable=False)
    editing_session_id = Column(String(36),nullable=False)


class WorkReviewPreview(Base):
    """明示確認した変換入力を保持する。本文・認証情報は格納しない。"""
    __tablename__ = 'work_review_previews'
    __table_args__ = ({'info': {'requires_explicit_migration': True}},)
    id = Column(String(36), primary_key=True)
    basis_id = Column(String(36), ForeignKey('work_bases.id', ondelete='RESTRICT'), nullable=False)
    submission_id = Column(String(36), ForeignKey('task_submissions.id', ondelete='RESTRICT'), nullable=False)
    input_digest = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)


class WorkEditingSession(Base):
    """管理側が発行する編集セッション。拡張のdirty申告や認証鍵は保存しない。"""
    __tablename__ = 'work_editing_sessions'
    __table_args__ = ({'info': {'requires_explicit_migration': True}},)
    id = Column(String(36), primary_key=True)
    basis_id = Column(String(36), ForeignKey('work_bases.id', ondelete='RESTRICT'), nullable=False)
    member_id = Column(Integer, ForeignKey('users.id', ondelete='RESTRICT'), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)


class WorkEnvironmentOperation(Base):
    """準備・最新版取得の受付証拠。応答不明でも別の環境を自動生成しない。"""
    __tablename__ = 'work_environment_operations'
    __table_args__ = (UniqueConstraint('member_id', 'request_id', name='uq_work_environment_request'),
                      {'info': {'requires_explicit_migration': True}})
    id = Column(String(36), primary_key=True)
    request_id = Column(String(36), nullable=False)
    member_id = Column(Integer, ForeignKey('users.id', ondelete='RESTRICT'), nullable=False)
    team_id = Column(Integer, nullable=False)
    project_id = Column(Integer, nullable=False)
    task_id = Column(Integer, nullable=False)
    previous_basis_id = Column(String(36), nullable=True)
    editing_session_id = Column(String(36), nullable=True)
    result_basis_id = Column(String(36), nullable=True)
    base_commit = Column(String(40), nullable=True)
    state = Column(String(24), nullable=False)


class WorkEditorAccess(Base):
    """ブラウザー用の短命なアクセス権。平文cookieはDBへ保存しない。"""
    __tablename__ = 'work_editor_access'
    __table_args__ = ({'info': {'requires_explicit_migration': True}},)
    token_digest = Column(String(64), primary_key=True)
    session_id = Column(String(36), ForeignKey('work_editing_sessions.id', ondelete='RESTRICT'), nullable=False)
    workspace_id = Column(String(36), nullable=False)
    expires_at = Column(DateTime, nullable=False)


class WorkWorkspaceRevision(Base):
    """管理側だけが更新する保存元世代。利用者のリクエストから更新しない。"""
    __tablename__ = 'work_workspace_revisions'
    __table_args__ = (CheckConstraint('generation > 0', name='ck_workspace_generation'),
                      {'info': {'requires_explicit_migration': True}})
    workspace_id = Column(String(36), primary_key=True)
    generation = Column(Integer, nullable=False)


class WorkCollectionDiagnostic(Base):
    """収集失敗の固定コードだけを保持する。内部例外の文字列は保存しない。"""
    __tablename__ = 'work_collection_diagnostics'
    __table_args__ = ({'info': {'requires_explicit_migration': True}},)
    submission_id = Column(String(36), ForeignKey('task_submissions.id', ondelete='RESTRICT'), primary_key=True)
    code = Column(String(64), nullable=False)


class TaskDeletionIntent(Base):
    """基準treeから発行した削除証拠。任意パスを削除許可にしない。"""
    __tablename__ = 'task_deletion_intents'
    __table_args__ = (UniqueConstraint('basis_id','submission_id','path',name='uq_task_deletion_intent'),
                      {'info': {'requires_explicit_migration': True}})
    id = Column(String(36), primary_key=True)
    basis_id = Column(String(36), ForeignKey('work_bases.id', ondelete='RESTRICT'), nullable=False)
    submission_id = Column(String(36), ForeignKey('task_submissions.id', ondelete='RESTRICT'), nullable=False)
    member_id = Column(Integer, nullable=False)
    project_id = Column(Integer, nullable=False)
    task_id = Column(Integer, nullable=False)
    path = Column(String(512), nullable=False)
    blob = Column(String(40), nullable=False)
    mode = Column(String(6), nullable=False)
    binding_version = Column(String(64), nullable=False)
    policy_version = Column(Integer, nullable=False)
    confirmation_id = Column(String(36), nullable=False)


class TaskReviewConversion(Base):
    """収集一件→最大一依頼。レビュー結果は既存ChangeRequest側にだけ置く。"""
    __tablename__ = 'task_review_conversions'
    __table_args__ = (CheckConstraint("state IN ('PREPARED','READY','NO_CHANGE','REVIEW_REQUIRED')",name='ck_task_review_conversion_state'),
                      {'info': {'requires_explicit_migration': True}})
    submission_id = Column(String(36), ForeignKey('task_submissions.id',ondelete='RESTRICT'), primary_key=True)
    basis_id = Column(String(36), ForeignKey('work_bases.id',ondelete='RESTRICT'), nullable=False)
    input_digest = Column(String(64), nullable=False)
    recipe = Column(Text, nullable=False)
    state = Column(String(24), nullable=False)
    generated_commit = Column(String(40), nullable=True)
    change_request_id = Column(Integer, ForeignKey('change_requests.id',ondelete='RESTRICT'), unique=True)
    previous_request_id = Column(Integer, ForeignKey('change_requests.id',ondelete='RESTRICT'), nullable=True)


class SubmissionPermit(Base):
    __tablename__ = "submission_permits"
    __table_args__ = (
        CheckConstraint("purpose = 'TASK_SUBMISSION'", name="ck_submission_permit_purpose"),
        CheckConstraint("expires_at > created_at", name="ck_submission_permit_expiry"),
        {"info": {"requires_explicit_migration": True}},
    )
    id = Column(String(36), primary_key=True)
    token_digest = Column(String(64), nullable=False, unique=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    # Historical identity: deliberately no cascading FK to Task/target.
    task_id = Column(Integer, nullable=False)
    editing_session_id = Column(String(36), nullable=False)
    folder_path = Column(String(512), nullable=False)
    purpose = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)


class TaskSubmission(Base):
    __tablename__ = "task_submissions"
    __table_args__ = (
        UniqueConstraint("student_id", "task_id", "request_id", name="uq_task_submission_request"),
        CheckConstraint("state IN ('PROCESSING','SUBMITTED','REVIEW_REQUIRED')", name="ck_task_submission_state"),
        CheckConstraint("state != 'SUBMITTED' OR (git_ref IS NOT NULL AND commit_id IS NOT NULL AND submitted_at IS NOT NULL AND file_count IS NOT NULL AND total_bytes IS NOT NULL)", name="ck_task_submission_receipt"),
        CheckConstraint("file_count IS NULL OR file_count BETWEEN 1 AND 1000", name="ck_task_submission_files"),
        CheckConstraint("total_bytes IS NULL OR total_bytes BETWEEN 0 AND 10485760", name="ck_task_submission_bytes"),
        {"info": {"requires_explicit_migration": True}},
    )
    id = Column(String(36), primary_key=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False)
    task_id = Column(Integer, nullable=False)
    request_id = Column(String(36), nullable=False)
    editing_session_id = Column(String(36), nullable=False)
    permit_id = Column(String(36), ForeignKey("submission_permits.id", ondelete="RESTRICT"), nullable=False, unique=True)
    permit_digest = Column(String(64), nullable=False)
    folder_path = Column(String(512), nullable=False)
    state = Column(String(24), nullable=False)
    git_ref = Column(String(100), nullable=True, unique=True)
    commit_id = Column(String(40), nullable=True)
    created_at = Column(DateTime, nullable=False)
    submitted_at = Column(DateTime, nullable=True)
    file_count = Column(Integer, nullable=True)
    total_bytes = Column(Integer, nullable=True)

