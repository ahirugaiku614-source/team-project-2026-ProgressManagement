"""Bounded task-only collection; injectable directory operations for fake tests."""
import stat
import os
import time
from app.submission_folders import DirectoryOps, open_submission_folder, _check_directory
from app.submission_scope import normalize_folder_path
from app.review_git import ReviewGit, ReviewInvalid, ReviewLimit
from app.project_storage import StorageConflict

MAX_FILES = 1000


class EmptyTaskCollection(ReviewInvalid):
    """空収集を変更なしと混同しないための型。本文やパスを保持しない。"""
    pass


MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 10 * MAX_FILE_BYTES
MAX_DEPTH = 20
MAX_ENTRIES = 2000
EXCLUDED = frozenset({'.git', '.gitignore', '.gitattributes', '.ssh', '.aws', '.venv', 'venv', 'node_modules',
    '__pycache__', 'dist', 'build', 'coverage', '.pytest_cache'})


def excluded(name):
    name = name.casefold()
    return (name in EXCLUDED or name == '.env' or name.startswith('.env.')
            or name.endswith(('.pem', '.key', '.pyc', '.pyo'))
            or name in {'firebase-credentials.json', 'id_rsa', 'id_ed25519'})


def fingerprint(info):
    return (*ReviewGit.fingerprint(info), info.st_uid)


class CollectionOps(DirectoryOps):
    def names(self, directory):
        # Never materialize an unbounded directory listing.
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > MAX_ENTRIES:
                    raise ReviewLimit('Task directory entry limit exceeded')
        return sorted(names)

    def entry_info(self, directory, name):
        return os.stat(name, dir_fd=directory, follow_symlinks=False)

    def read_file(self, directory, name, expected):
        # Reuse O_NOFOLLOW, O_NONBLOCK, bounded reads and fstat comparisons.
        return ReviewGit(None).read(directory, name, MAX_FILE_BYTES, expected)


def collect_task(request, root, ops):
    """Return stable bytes relative to the registered folder, never its siblings.

    Two complete observations detect ordinary concurrent editing, not an atomic
    filesystem snapshot against an adversarial writer or privileged mount changes.
    """
    deadline = time.monotonic() + 30
    def observe(directory):
        files, observed = {}, []
        total = scanned = 0
        def visit(fd, prefix='', depth=0):
            nonlocal total, scanned
            if depth > MAX_DEPTH:
                raise ReviewLimit('Task depth limit exceeded')
            before = ops.info(fd)
            _check_directory(before, root)
            names = ops.names(fd)
            if len(names) != len(set(names)):
                raise ReviewInvalid('Duplicate task directory entries')
            for name in sorted(names):
                if time.monotonic() > deadline:
                    raise ReviewLimit('Task collection deadline exceeded')
                scanned += 1
                if scanned > MAX_ENTRIES:
                    raise ReviewLimit('Task entry limit exceeded')
                if not isinstance(name, str) or '/' in name or '\\' in name or name in ('', '.', '..'):
                    raise ReviewInvalid('Unsafe task entry')
                info = ops.entry_info(fd, name)
                if (info.st_dev != root.device or info.st_uid != root.owner_uid
                        or info.st_mode & 0o022):
                    raise ReviewInvalid('Unsafe task entry identity')
                if not stat.S_ISDIR(info.st_mode) and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                    raise ReviewInvalid('Links and special files cannot be submitted')
                if excluded(name):
                    continue
                path = normalize_folder_path(prefix + name)
                if stat.S_ISDIR(info.st_mode):
                    child = ops.open_directory(fd, name)
                    try:
                        if fingerprint(ops.info(child)) != fingerprint(info):
                            raise StorageConflict('Task directory identity changed')
                        visit(child, path + '/', depth + 1)
                    finally:
                        ops.close(child)
                    if fingerprint(ops.entry_info(fd, name)) != fingerprint(info):
                        raise StorageConflict('Task directory entry changed')
                else:
                    if len(files) >= MAX_FILES or info.st_size > MAX_FILE_BYTES:
                        raise ReviewLimit('Task file limit exceeded')
                    content = ops.read_file(fd, name, info)
                    if not isinstance(content, bytes) or len(content) != info.st_size or len(content) > MAX_FILE_BYTES:
                        raise StorageConflict('Task file changed')
                    if fingerprint(info) != fingerprint(ops.entry_info(fd, name)):
                        raise StorageConflict('Task file identity changed')
                    total += len(content)
                    if total > MAX_TOTAL_BYTES:
                        raise ReviewLimit('Task byte limit exceeded')
                    # Data only; executable bits do not cause code execution.
                    files[path] = ('100644', content)
                    observed.append((path, fingerprint(info)))
            if (names != ops.names(fd) or fingerprint(before) != fingerprint(ops.info(fd))):
                raise StorageConflict('Task directory changed')
        visit(directory)
        return files, observed
    with open_submission_folder(request.target, root, ops=ops) as folder:
        first = observe(folder)
        if first != observe(folder):
            raise StorageConflict('Task changed during collection')
        if not first[0]:
            raise EmptyTaskCollection('Task contains no submission files')
        return first[0]
