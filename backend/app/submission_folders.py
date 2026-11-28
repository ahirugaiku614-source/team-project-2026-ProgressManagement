"""管理側が保持するroot FDからのディレクトリ識別だけ。本文は開かない。

root FD/識別情報は認証済みUser×Projectに対応する管理側providerが供給する。
HTTPから絶対パスやFDを受け付けない。providerの実環境への接続は今後の工程。
"""

from contextlib import contextmanager
from dataclasses import dataclass
import os
import stat
import sys

from app.submission_scope import SubmissionTarget, TargetForbidden, TargetInvalid, normalize_folder_path, positive_id


class FolderUnavailable(RuntimeError):
    pass


class DirectoryOps:
    def __init__(self):
        if (sys.platform != "linux" or not hasattr(os, "O_NOFOLLOW")
                or not hasattr(os, "O_DIRECTORY") or os.open not in os.supports_dir_fd):
            raise FolderUnavailable("Secure folder inspection requires Linux")

    def duplicate(self, fd):
        return os.dup(fd)

    def open_directory(self, parent, name):
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)

    def info(self, fd):
        return os.fstat(fd)

    def close(self, fd):
        os.close(fd)


@dataclass(frozen=True)
class ManagedTaskRoot:
    team_id: int
    project_id: int
    student_id: int
    descriptor: int
    device: int
    inode: int
    owner_uid: int

    def __post_init__(self):
        for value in (self.team_id, self.project_id, self.student_id):
            positive_id(value)
        if (type(self.descriptor) is not int or self.descriptor < 0
                or any(type(v) is not int or v < 0 for v in (self.device, self.inode, self.owner_uid))):
            raise TargetInvalid("Invalid managed root identity")


def _identity(info):
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode


def _check_directory(info, root):
    if (not stat.S_ISDIR(info.st_mode) or info.st_dev != root.device
            or info.st_uid != root.owner_uid or info.st_mode & 0o022):
        raise TargetInvalid("Unsafe task directory")


@contextmanager
def open_submission_folder(target, root, *, ops=None):
    """FDを保持して相対走査する。返したFDで後から本文を収集する機能は未実装。

認可を保存する機能ではない。将来の提出処理は操作ごとにDB認可を再解決する。
FDの安定性は領域全体の同時点スナップショット/特権者によるmount変更の保証ではない。
"""
    if not isinstance(target, SubmissionTarget) or not isinstance(root, ManagedTaskRoot):
        raise TargetInvalid("Managed task binding required")
    if (target.team_id, target.project_id, target.student_id) != (root.team_id, root.project_id, root.student_id):
        raise TargetForbidden("Managed root belongs to another submission scope")
    parts = normalize_folder_path(target.folder_path).split("/")
    access = ops if ops is not None else DirectoryOps()
    handles, identities = [], []
    try:
        parent = access.duplicate(root.descriptor)
        handles.append(parent)
        info = access.info(parent)
        _check_directory(info, root)
        if (info.st_dev, info.st_ino) != (root.device, root.inode):
            raise TargetInvalid("Managed root identity changed")
        identities.append(_identity(info))
        for part in parts:
            parent = access.open_directory(parent, part)
            handles.append(parent)
            info = access.info(parent)
            _check_directory(info, root)
            identities.append(_identity(info))
        # 操作直前に保持中の全FDを再照合。パスを再解決して開き直さない。
        if any(_identity(access.info(fd)) != expected for fd, expected in zip(handles, identities)):
            raise TargetInvalid("Task directory changed during inspection")
        yield parent
    except OSError:
        raise TargetInvalid("Task directory cannot be safely inspected") from None
    finally:
        # 一つのcloseが失敗しても他のFDを閉じる。元のroot descriptorは所有しない。
        failure = False
        for fd in reversed(handles):
            try:
                access.close(fd)
            except OSError:
                failure = True
        if failure:
            raise FolderUnavailable("Task directory handle cleanup failed")
