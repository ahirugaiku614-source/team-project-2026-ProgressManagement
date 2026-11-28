"""DB・Firebase・実接続から独立した作業環境証拠。管理ワーカーでも共有する。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspaceInitialization:
    basis_id: str
    target: object
    base_commit: str
    workspace_id: str
    source_generation: int
