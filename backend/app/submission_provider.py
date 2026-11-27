"""Managed collector contract. No filesystem, Git, process or network implementation."""
from dataclasses import dataclass
from typing import Protocol

from app.submission_scope import SubmissionTarget


class ProviderUnavailable(RuntimeError):
    pass


class EvidenceMismatch(ProviderUnavailable):
    """保存済み証拠の欠落・矛盾を実際に確認した場合だけ使用する。

    接続失敗、ロック待ち、読取り失敗、認可拒否、未知の例外はこの型に変換しない。
    呼出し側は照合不能と確定した不整合を別々に処理する。
    """


@dataclass(frozen=True)
class SubmissionCollection:
    submission_id: str
    request_id: str
    editing_session_id: str
    target: SubmissionTarget


@dataclass(frozen=True)
class SubmissionSnapshot:
    """Receipt for an immutable snapshot, not a client supplied file upload.

    Collector must independently verify the editing session, absence of dirty
    documents, stable saved files, limits/links and persist the immutable snapshot.
    Returning a receipt without those checks violates this trusted interface.
    """
    submission_id: str
    target: SubmissionTarget
    editing_session_id: str
    git_ref: str
    commit_id: str
    file_count: int
    total_bytes: int
    saved_files_confirmed: bool


class SubmissionProvider(Protocol):
    """verify/recoverは既存証拠の照合であり、新しい収集を許可しない。

    新規収集の公開には、任意の専用verify_collectionがあればsubmitがそれを使う。
    専用検査を持たない既存providerは、従来どおりverifyで公開条件も検証する。
    いずれも信頼済みサーバー側の実装境界で、HTTP入力から検査を選択できない。
    """
    def ensure_ready(self) -> None: ...
    def collect(self, request: SubmissionCollection) -> SubmissionSnapshot: ...
    def verify(self, request: SubmissionCollection, receipt: SubmissionSnapshot) -> None: ...
    def recover(self, request: SubmissionCollection) -> SubmissionSnapshot: ...


class DeniedSubmissionProvider:
    def ensure_ready(self):
        raise ProviderUnavailable("Submission collector is not connected")

    def collect(self, request):
        raise ProviderUnavailable("Submission collector is not connected")

    def verify(self, request, receipt):
        raise ProviderUnavailable("Submission collector is not connected")

    def recover(self, request):
        raise ProviderUnavailable("Submission collector is not connected")


def get_submission_provider():
    # Only explicit FastAPI dependency overrides in tests replace this provider.
    return DeniedSubmissionProvider()
