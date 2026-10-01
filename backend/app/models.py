from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, Enum
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from app.db.database import Base

# タスクのステータス定義
class TaskStatus(str, enum.Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"

# 1. ユーザーテーブル
class User(Base):
    __tablename__ = "users"

    id = Column(Integer,primary_key=True, index=True)
    firebase_uid = Column(String, unique=True, index=True, nullable=False)  # FirebaseのUID
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # リレーション
    tasks = relationship("Task", back_populates="assignee")

# 2. チームテーブル
class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # リレーション
    tasks = relationship("Task", back_populates="team")

# 3. タスクテーブル
class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(Enum(TaskStatus), default=TaskStatus.TODO, nullable=False)
    
    # 外部キー
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False)
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # リレーション
    team = relationship("Team", back_populates="tasks")
    assignee = relationship("User", back_populates="tasks")