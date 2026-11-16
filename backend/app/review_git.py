"""提出用Git処理。利用者の.gitをGitコマンドで開かないことが重要。"""

from contextlib import contextmanager
import difflib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid

from app.project_storage import StorageConflict, StorageUnavailable

MAX_FILES = 1000
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 10 * MAX_FILE_BYTES
MAX_PATCH_BYTES = 256 * 1024
OID = re.compile(r"[0-9a-f]{40}\Z")


class ReviewLimit(Exception):
    pass


class ReviewInvalid(Exception):
    pass


def valid_path(path):
    """Git出力にも同じ検査を適用し、制御文字や特殊なパスを返さない。"""
    parts = path.split("/")
    if (len(parts) > 20 or len(path.encode("utf-8")) > 512 or "\\" in path
            or any(p in ("", ".", "..") or p.casefold() == ".git" for p in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise ReviewInvalid("Unsafe review file path")
    return path


class ReviewGit:
    def __init__(self, storage):
        self.storage = storage

    @contextmanager
    def new_directory(self, parent, name):
        with self.storage._directory(parent, name, create=True) as directory:
            # 新しいディレクトリ名も永続化し、電源断時に親だけ失われることを避ける。
            os.fsync(parent)
            yield directory

    @contextmanager
    def session(self, project_id):
        """既存の初期化ロックを再利用し、初期化・提出をプロジェクト単位で直列化する。"""
        try:
            with self.storage._project(project_id) as project, self.storage._lock(project):
                self.deadline = time.monotonic() + 60
                if self.storage._state(project, "shared.git", self.storage._identity(project_id), True) != "ready":
                    raise StorageConflict("Initialize the project repository first")
                with self.storage._directory(project, "shared.git") as shared:
                    with self.storage._directory(shared, "objects") as objects:
                        with self.storage._directory(objects, "info") as info:
                            for name in ("alternates", "http-alternates"):
                                try:
                                    os.stat(name, dir_fd=info, follow_symlinks=False)
                                except FileNotFoundError:
                                    continue
                                raise StorageConflict("External Git object stores are not allowed")
                        # 新規の一時bareリポジトリの設定だけを使う。共有側のconfigも読み込まない。
                        with tempfile.TemporaryDirectory(prefix="review-git-") as scratch:
                            self.scratch, self.objects = scratch, objects
                            self.run("init", "--bare", "--object-format=sha1", "--template=", scratch, initial=True)
                            self.project, self.shared = project, shared
                            yield self
        except FileNotFoundError as error:
            raise StorageConflict("Initialize your project workspace first") from error

    def run(self, *arguments, data=b"", initial=False):
        self.check_deadline()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
            "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "", "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1", "GIT_AUTHOR_NAME": "Project Review", "GIT_AUTHOR_EMAIL": "review@localhost",
            "GIT_COMMITTER_NAME": "Project Review", "GIT_COMMITTER_EMAIL": "review@localhost",
            "GIT_AUTHOR_DATE": "@0 +0000" if initial else "@" + str(int(time.time())) + " +0000",
            "GIT_COMMITTER_DATE": "@0 +0000" if initial else "@" + str(int(time.time())) + " +0000",
        }
        if not initial:
            env.update(GIT_DIR=self.scratch, GIT_OBJECT_DIRECTORY=f"/proc/self/fd/{self.objects}")
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
                   "-c", "core.fsync=all", "-c", "gc.auto=0", "-c", "maintenance.auto=false", *arguments]
        try:
            # 出力を一時ファイルへ出し、Git出力でPythonのメモリを使い切らない。
            with tempfile.TemporaryFile() as output:
                subprocess.run(command, input=data, stdout=output, stderr=subprocess.DEVNULL,
                               env=env, cwd=self.scratch, pass_fds=(self.objects,),
                               timeout=min(30, max(0.1, self.deadline - time.monotonic())), check=True, shell=False)
                if output.tell() > MAX_TOTAL_BYTES:
                    raise ReviewLimit("Git output exceeds the review limit")
                output.seek(0)
                return output.read()
        except (OSError, subprocess.SubprocessError) as error:
            raise StorageUnavailable("Review Git operation failed; retry with the same request key") from error

    def check_deadline(self):
        if time.monotonic() > self.deadline:
            raise StorageUnavailable("Review operation timed out; retry with the same request key")

    @staticmethod
    def oid(value):
        if not isinstance(value, str) or not OID.fullmatch(value):
            raise StorageConflict("Invalid review Git object")
        return value

    def commit_exists(self, oid):
        if self.run("cat-file", "-t", self.oid(oid)).strip() != b"commit":
            raise StorageConflict("Review commit is unavailable")

    @staticmethod
    def fingerprint(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_nlink)

    def read(self, directory, name, limit=65536, expected=None):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise StorageConflict("Unsafe review storage file")
            if expected is not None and self.fingerprint(info) != self.fingerprint(expected):
                raise StorageConflict("Workspace changed during submission; retry")
            if info.st_size > limit:
                raise ReviewLimit("Review storage file exceeds the limit")
            result = bytearray()
            while chunk := os.read(fd, min(65536, limit + 1 - len(result))):
                result.extend(chunk)
                if len(result) > limit:
                    raise ReviewLimit("Review storage file exceeds the limit")
            if self.fingerprint(info) != self.fingerprint(os.fstat(fd)):
                raise StorageConflict("File changed during review operation; retry")
            return bytes(result)
        finally:
            os.close(fd)

    def write_once(self, directory, name, content):
        """正式ファイルは上書きしない。書き終えた新規ファイルだけを原子的に公開する。"""
        try:
            if self.read(directory, name) != content:
                raise StorageConflict("Existing review record does not match")
            return
        except FileNotFoundError:
            pass
        stage = ".pending-" + uuid.uuid4().hex
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        self.storage._publish(directory, stage, name)

    def main_commit(self):
        value = self.read_ref(("heads", "main"))
        if value:
            return value
        # 共有mainがまだない場合も、比較元を不変の空コミットとして保持する。
        empty = self.run("mktree", "-z").decode().strip()
        return self.run("commit-tree", empty, data=b"Empty review base\n").decode().strip()

    def read_ref(self, parts):
        from contextlib import ExitStack
        if not parts or any(not isinstance(part, str) or not part or "/" in part for part in parts):
            raise StorageConflict("Invalid Git reference name")
        name = b"refs/" + b"/".join(part.encode("utf-8") for part in parts)
        if not self.valid_ref_name(name):
            raise StorageConflict("Invalid Git reference name")
        loose = None
        try:
            with ExitStack() as stack:
                directory = stack.enter_context(self.storage._directory(self.shared, "refs"))
                for part in parts[:-1]:
                    directory = stack.enter_context(self.storage._directory(directory, part))
                raw = self.read(directory, parts[-1]).removesuffix(b"\n")
                if not re.fullmatch(rb"[0-9a-f]{40}", raw) or raw == b"0" * 40:
                    raise StorageConflict("Invalid loose Git reference")
                loose = raw.decode("ascii")
        except FileNotFoundError:
            pass

        packed = self.packed_refs().get(name)
        # mainは更新されるためGitと同じくloose優先。不変のレビュー参照は両方を照合する。
        if name.startswith(b"refs/change-requests/") and loose and packed and loose != packed:
            raise StorageConflict("Loose and packed review references do not match")
        return loose if loose is not None else packed

    @staticmethod
    def valid_ref_name(name):
        # UTF-8以外の既存参照名もbytesのまま扱い、ASCII制御文字やGitの禁止形式だけを拒否する。
        return (name.startswith(b"refs/") and not name.endswith(b".")
                and b".." not in name and b"@{" not in name
                and not any(c <= 32 or c == 127 or c in b"~^:?*[\\" for c in name)
                and all(part and not part.startswith(b".") and not part.endswith(b".lock")
                        for part in name.split(b"/")))

    def packed_refs(self):
        try:
            raw = self.read(self.shared, "packed-refs", MAX_FILE_BYTES)
        except FileNotFoundError:
            return {}
        if raw and not raw.endswith(b"\n"):
            raise StorageConflict("Unterminated packed Git reference")
        result, previous, can_peel, sorted_refs, header = {}, None, False, False, False
        for line in raw.split(b"\n")[:-1]:
            self.check_deadline()
            if line.startswith(b"# pack-refs with:") and previous is None and not header:
                sorted_refs = b"sorted" in line.split(b":", 1)[1].split()
                header = True
                continue
            if line.startswith(b"^"):
                if not can_peel or not re.fullmatch(rb"\^[0-9a-f]{40}", line) or line[1:] == b"0" * 40:
                    raise StorageConflict("Invalid peeled Git reference")
                can_peel = False
                continue
            match = re.fullmatch(rb"([0-9a-f]{40}) (.+)", line)
            if not match or match[1] == b"0" * 40 or not self.valid_ref_name(match[2]):
                raise StorageConflict("Invalid packed Git reference")
            oid, name = match.groups()
            if name in result or (sorted_refs and previous is not None and name <= previous):
                raise StorageConflict("Duplicate or unsorted packed Git references")
            result[name] = oid.decode("ascii")
            previous, can_peel = name, True
        return result

    @contextmanager
    def git_lock(self, directory, name):
        """Git標準の.lockも取得する。既存ロックは削除せず、今回作成したものだけを解放する。"""
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        except FileExistsError as error:
            raise StorageUnavailable("Git reference is locked; retry later") from error
        created = os.fstat(fd)
        try:
            yield
        finally:
            try:
                try:
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError as error:
                    raise StorageConflict("Git lock disappeared; operator inspection required") from error
                if ((current.st_dev, current.st_ino) != (created.st_dev, created.st_ino)
                        or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                    raise StorageConflict("Git lock ownership changed; operator inspection required")
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(fd)

    def capture(self, user_id, project_id):
        with self.storage._directory(self.project, "workspaces") as workspaces:
            with self.storage._directory(workspaces, self.storage._id(user_id)) as workspace:
                with self.storage._directory(workspace, ".git") as git:
                    try:
                        marker = json.loads(self.storage._read(git, ".storage-ready.json"))
                    except (ValueError, UnicodeError) as error:
                        raise StorageConflict("Workspace identity is invalid") from error
                    if marker != self.storage._identity(project_id, user_id):
                        raise StorageConflict("Workspace identity does not match")
                first = self._files(workspace)
                # 編集中のファイルを混ぜないよう、内容とメタデータを二回照合する。
                if first != self._files(workspace):
                    raise StorageConflict("Workspace changed during submission; retry")
                return {path: (mode, content) for path, (mode, content, _) in first.items()}

    def _files(self, root):
        result, total, scanned = {}, 0, 0

        def visit(directory, prefix="", depth=0):
            nonlocal total, scanned
            if depth > 20:
                raise ReviewLimit("Workspace directory depth exceeds the limit")
            before = os.fstat(directory)
            for name in sorted(os.listdir(directory)):
                self.check_deadline()
                if not prefix and name == ".git":
                    continue
                scanned += 1
                if scanned > 2000:
                    raise ReviewLimit("Workspace entry count exceeds the limit")
                try:
                    path = valid_path(prefix + name)
                except UnicodeError as error:
                    raise ReviewInvalid("File names must be valid UTF-8") from error
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    with self.storage._directory(directory, name) as child:
                        visit(child, path + "/", depth + 1)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ReviewInvalid("Symlinks, hard links and special files cannot be submitted")
                content = self.read(directory, name, MAX_FILE_BYTES, expected=info)
                after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if self.fingerprint(info) != self.fingerprint(after):
                    raise StorageConflict("Workspace changed during submission; retry")
                total += len(content)
                if total > MAX_TOTAL_BYTES or len(result) >= MAX_FILES:
                    raise ReviewLimit("Workspace exceeds the review limit")
                result[path] = ("100755" if info.st_mode & 0o111 else "100644", content, self.fingerprint(after))
            after = os.fstat(directory)
            if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
                raise StorageConflict("Workspace changed during submission; retry")
        visit(root)
        return result

    def tree(self, files):
        nested = {}
        for path, (mode, content) in files.items():
            current = nested
            parts = path.split("/")
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            oid = self.run("hash-object", "-w", "--stdin", data=content).decode().strip()
            current[parts[-1]] = (mode, oid)

        def build(node):
            lines = []
            for name, entry in sorted(node.items()):
                mode, oid = ("040000", build(entry)) if isinstance(entry, dict) else entry
                kind = "tree" if mode == "040000" else "blob"
                lines.append(f"{mode} {kind} {oid}\t{name}\0".encode("utf-8"))
            return self.run("mktree", "-z", data=b"".join(lines)).decode().strip()
        return build(nested)

    def entries(self, commit):
        self.commit_exists(commit)
        entries = {}
        for record in self.run("ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
            if not record:
                continue
            meta, path = record.split(b"\t", 1)
            mode, kind, oid = meta.decode("ascii").split()
            if mode not in ("100644", "100755") or kind != "blob":
                raise ReviewInvalid("Unsupported file type in review history")
            path = valid_path(path.decode("utf-8"))
            entries[path] = (mode, self.oid(oid))
            if len(entries) > MAX_FILES:
                raise ReviewLimit("Review file count exceeds the limit")
        return entries

    def blob(self, oid):
        if int(self.run("cat-file", "-s", self.oid(oid))) > MAX_FILE_BYTES:
            raise ReviewLimit("Review file exceeds the size limit")
        return self.run("cat-file", "blob", oid)

    @contextmanager
    def locked_review_refs(self, ref_key):
        """base/headの検査から処理確定まで、外部の通常Git操作にもロックを示す。

        参照作成だけでなく判定時の読み取りにも使う。packed-onlyの場合には
        ロック配置用ディレクトリだけを準備し、正式な参照は作成・変更しない。
        """
        from contextlib import ExitStack
        if not isinstance(ref_key, str) or not re.fullmatch(r"[0-9a-f]{32}", ref_key):
            raise StorageConflict("Invalid review reference key")
        with self.git_lock(self.shared, "packed-refs.lock"), self.storage._directory(self.shared, "refs") as refs:
            with self.new_directory(refs, "change-requests") as reviews:
                with self.new_directory(reviews, ref_key) as review, ExitStack() as locks:
                    for name in ("base", "head"):
                        locks.enter_context(self.git_lock(review, name + ".lock"))
                    yield review

    def ensure_refs(self, manifest):
        for key in ("base_commit", "snapshot_commit"):
            self.commit_exists(manifest[key])
        ref_key = manifest.get("ref_key")
        with self.locked_review_refs(ref_key) as review:
            # 両方の検査が通るまで書き込まない。片方が不一致なら他方の欠落も補わない。
            missing = []
            for name, key in (("base", "base_commit"), ("head", "snapshot_commit")):
                actual = self.read_ref(("change-requests", ref_key, name))
                if actual is not None and actual != manifest[key]:
                    raise StorageConflict("Existing review reference does not match")
                if actual is None:
                    missing.append((name, key))
            for name, key in missing:
                self.write_once(review, name, (manifest[key] + "\n").encode())

    def manifest(self, ref_key):
        if not re.fullmatch(r"[0-9a-f]{32}", ref_key):
            raise StorageConflict("Invalid review reference key")
        try:
            with self.storage._directory(self.project, "review-journal") as journal:
                return json.loads(self.read(journal, ref_key + ".json"))
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError) as error:
            raise StorageConflict("Review journal is invalid; operator inspection required") from error

    def save_manifest(self, manifest):
        with self.new_directory(self.project, "review-journal") as journal:
            self.write_once(journal, manifest["ref_key"] + ".json",
                            json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode())

    def verify_refs(self, manifest):
        for name, key in (("base", "base_commit"), ("head", "snapshot_commit")):
            actual = self.read_ref(("change-requests", manifest["ref_key"], name))
            if actual is None:
                raise StorageUnavailable("Review references missing; retry the original submission")
            if actual != manifest[key]:
                raise StorageConflict("Review reference does not match; operator inspection required")

    def diff(self, manifest):
        self.verify_refs(manifest)
        old, new = self.entries(manifest["base_commit"]), self.entries(manifest["snapshot_commit"])
        files, budget, total = [], MAX_PATCH_BYTES, 0
        for path in sorted(old.keys() | new.keys()):
            self.check_deadline()
            if old.get(path) == new.get(path):
                continue
            before = self.blob(old[path][1]) if path in old else b""
            after = self.blob(new[path][1]) if path in new else b""
            total += len(before) + len(after)
            if total > 2 * MAX_TOTAL_BYTES:
                raise ReviewLimit("Review diff input exceeds the limit")
            try:
                left, right = before.decode("utf-8"), after.decode("utf-8")
                binary = any(ord(c) < 32 and c not in "\n\r\t" for c in left + right)
            except UnicodeError:
                left = right = ""
                binary = True
            patch, truncated = None, False
            if not binary:
                if budget <= 0 or len(before) + len(after) > MAX_PATCH_BYTES or left.count("\n") + right.count("\n") > 4000:
                    truncated = True
                else:
                    lines = difflib.unified_diff(left.splitlines(keepends=True), right.splitlines(keepends=True),
                                                 fromfile="a/" + path, tofile="b/" + path)
                    patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                                    for line in lines)
                    if len(patch.encode()) > budget:
                        patch, truncated = None, True
                    else:
                        budget -= len(patch.encode())
            files.append({"path": path, "change": "A" if path not in old else "D" if path not in new else "M",
                          "binary": binary, "mode_changed": path in old and path in new and old[path][0] != new[path][0],
                          "patch": patch, "truncated": truncated})
        return {"base_commit": manifest["base_commit"], "snapshot_commit": manifest["snapshot_commit"],
                "files": files, "truncated": any(f["truncated"] for f in files)}
