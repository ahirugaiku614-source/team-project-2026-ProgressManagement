"""Linux-only, backend-owned project storage. Never mount it into user editors.

All traversal uses directory descriptors and O_NOFOLLOW. Publication uses Linux
renameat2(RENAME_NOREPLACE), so even an unexpected destination is never replaced.
Failed staging directories are retained for inspection; retries use new names.
"""

import ctypes
import errno
import json
import os
from pathlib import PurePosixPath
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager


class StorageConflict(Exception):
    pass


class StorageUnavailable(Exception):
    pass


class ProjectStorage:
    def __init__(self, root):
        path = PurePosixPath(root)
        if not path.is_absolute() or ".." in path.parts or str(path) == "/":
            raise StorageUnavailable("Invalid project storage configuration")
        if sys.platform != "linux":
            raise StorageUnavailable("Project storage requires Linux")
        self.root = path

    @staticmethod
    def _id(value):
        if type(value) is not int or value <= 0:
            raise StorageConflict("Invalid storage identifier")
        return str(value)

    @contextmanager
    def _directory(self, parent, name, create=False):
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent,
            )
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise StorageConflict("Unsafe storage directory") from error
            raise
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    @contextmanager
    def _root(self, create=False):
        from contextlib import ExitStack
        with ExitStack() as stack:
            current = stack.enter_context(self._directory(None, "/"))
            for component in self.root.parts[1:]:
                current = stack.enter_context(self._directory(current, component, create))
            yield current

    @contextmanager
    def _project(self, project_id, create=False):
        with self._root(create) as root:
            with self._directory(root, "projects", create) as projects:
                with self._directory(projects, self._id(project_id), create) as project:
                    yield project

    @contextmanager
    def _lock(self, project, *, wait=True):
        import fcntl
        try:
            fd = os.open(".init.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=project)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise StorageConflict("Unsafe storage lock") from error
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StorageConflict("Unsafe storage lock")
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    # レビュー経路はDBロックを保持して入る場合がある。Gitロックを
                    # 待たずに返し、Git→DBとの循環待ちを作らない。初期化は従来どおり。
                    if not wait or time.monotonic() >= deadline:
                        raise StorageUnavailable("Project initialization is busy; retry later")
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _read(directory, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise StorageConflict("Unsafe storage file") from error
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
                raise StorageConflict("Unsafe storage file")
            return os.read(fd, 16385).decode("utf-8")
        finally:
            os.close(fd)

    def _check_tree(self, directory, sync=False):
        # Reject symlinks (including dangling ones), special files and hard links
        # anywhere inside a published repository, without following them.
        for name in os.listdir(directory):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                with self._directory(directory, name) as child:
                    self._check_tree(child, sync)
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StorageConflict("Unsafe repository contents")
            elif sync:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                        raise StorageConflict("Unsafe repository contents")
                    os.fsync(fd)
                finally:
                    os.close(fd)
        if sync:
            os.fsync(directory)

    def _state(self, parent, name, identity, bare):
        try:
            with self._directory(parent, name) as directory:
                self._check_tree(directory)
                try:
                    with self._directory(directory, "." if bare else ".git") as git:
                        marker = json.loads(self._read(git, ".storage-ready.json"))
                        if marker != identity:
                            return "incomplete"
                        self._read(git, "HEAD")
                        self._read(git, "config")
                        for required in ("objects", "refs"):
                            with self._directory(git, required):
                                pass
                    return "ready"
                except (FileNotFoundError, ValueError, UnicodeError):
                    return "incomplete"
        except FileNotFoundError:
            return "missing"

    @staticmethod
    def _git(directory, *arguments):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
        try:
            subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", *arguments],
                cwd=f"/proc/self/fd/{directory}", pass_fds=(directory,), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, check=True, shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise StorageUnavailable("Git initialization failed; retry later") from error

    @staticmethod
    def _publish(parent, source, destination):
        # Linux atomic publication that cannot overwrite even an empty directory.
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            rename = libc.renameat2
        except AttributeError as error:
            raise StorageUnavailable("Atomic storage publication is unavailable") from error
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(parent, source.encode(), parent, destination.encode(), 1) != 0:
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise StorageConflict("Storage destination already exists")
            raise StorageUnavailable("Atomic storage publication failed")
        os.fsync(parent)

    def _ensure(self, parent, name, identity, bare, remote=None):
        state = self._state(parent, name, identity, bare)
        if state == "ready":
            return
        if state != "missing":
            raise StorageConflict("Existing storage is incomplete; operator inspection required")
        stage = ".staging-" + uuid.uuid4().hex
        os.mkdir(stage, mode=0o700, dir_fd=parent)
        with self._directory(parent, stage) as directory:
            arguments = ["init", "--initial-branch=main", "--template="]
            if bare:
                arguments.append("--bare")
            self._git(directory, *arguments, ".")
            if remote:
                self._git(directory, "remote", "add", "origin", remote)
            self._check_tree(directory, sync=True)
            with self._directory(directory, "." if bare else ".git") as git:
                fd = os.open(".storage-ready.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=git)
                with os.fdopen(fd, "w", encoding="utf-8") as marker:
                    json.dump(identity, marker)
                    marker.flush()
                    os.fsync(marker.fileno())
                os.fsync(git)
            os.fsync(directory)
        self._publish(parent, stage, name)

    @staticmethod
    def _identity(project_id, user_id=None):
        return {"version": 1, "project_id": project_id, "user_id": user_id}

    def initialize(self, project_id, user_id=None):
        self._id(project_id)
        if user_id is not None:
            self._id(user_id)
        with self._project(project_id, create=True) as project:
            with self._lock(project):
                self._ensure(project, "shared.git", self._identity(project_id), bare=True)
                if user_id is not None:
                    with self._directory(project, "workspaces", create=True) as workspaces:
                        self._ensure(
                            workspaces, str(user_id), self._identity(project_id, user_id), bare=False,
                            remote=str(self.root / "projects" / str(project_id) / "shared.git"),
                        )
        return self.status(project_id, user_id)

    def status(self, project_id, user_id=None):
        self._id(project_id)
        if user_id is not None:
            self._id(user_id)
        repository = workspace = "missing"
        try:
            with self._project(project_id) as project:
                repository = self._state(project, "shared.git", self._identity(project_id), bare=True)
                if user_id is not None:
                    try:
                        with self._directory(project, "workspaces") as workspaces:
                            workspace = self._state(workspaces, str(user_id), self._identity(project_id, user_id), bare=False)
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            pass
        result = {"project_id": project_id, "repository_state": repository, "default_branch": "main"}
        if user_id is not None:
            result.update(user_id=user_id, workspace_state=workspace)
        return result


def get_project_storage():
    try:
        return ProjectStorage(os.environ.get("PROJECT_STORAGE_ROOT", "/var/lib/team-projects"))
    except StorageUnavailable as error:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(error)) from error
