"""DB非依存の提出対象・パス契約。

収集側にDB資格情報やSQLAlchemy初期化を要求しないため、値型と純粋な検査を分離する。
既存submission_targetsからも同じ型を再公開するので、APIと例外の契約は変わらない。
"""
from dataclasses import dataclass
from pathlib import PurePosixPath
import unicodedata


class TargetInvalid(ValueError):
    pass


class TargetForbidden(PermissionError):
    pass


class TargetConflict(RuntimeError):
    pass


def positive_id(value):
    if type(value) is not int or not 0 < value <= 2**31 - 1:
        raise TargetInvalid("Invalid target identifier")
    return value


def normalize_folder_path(value):
    """NFC/POSIX正規形だけを受理する。危険な別名を黙って正規化しない。"""
    if not isinstance(value, str) or not value:
        raise TargetInvalid("Invalid task folder")
    try:
        if len(value.encode("utf-8")) > 512:
            raise TargetInvalid("Task folder exceeds limit")
    except UnicodeError:
        raise TargetInvalid("Invalid task folder") from None
    parts = value.split("/")
    if (len(parts) > 20 or value != unicodedata.normalize("NFC", value)
            or PurePosixPath(value).is_absolute() or str(PurePosixPath(value)) != value
            or any(not part or part in (".", "..") or part.startswith(".") for part in parts)
            or any(len(part.encode("utf-8")) > 255 for part in parts)
            or any(not (c.isalnum() or c in "_-/.") for c in value)):
        raise TargetInvalid("Invalid task folder")
    return value


@dataclass(frozen=True)
class SubmissionTarget:
    team_id: int
    project_id: int
    task_id: int
    student_id: int
    folder_path: str

    def __post_init__(self):
        for value in (self.team_id, self.project_id, self.task_id, self.student_id):
            positive_id(value)
        normalize_folder_path(self.folder_path)
