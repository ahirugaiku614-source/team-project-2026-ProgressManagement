"""新規世代だけを作るLinux用初期化アダプター。既定dependencyには接続しない。

Stage 10承認後、管理側専用rootとメンバーUID/GIDを固定して使用する。
メンバー側へbind mountするのはw-UUIDディレクトリだけであり、隣接する証拠は
渡さない。既存workspaceの上書き、削除、Git実行、利用者コード実行は行わない。
"""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
import stat
import sys
import time
from uuid import UUID, uuid4

from app.workspace_scope import WorkspaceInitialization
from app.submission_provider import ProviderUnavailable
from app.review_git import valid_path


class MappedWorkspaces:
    """管理側の固定member×Project対応だけを選択する。HTTPから設定を追加しない。"""
    def __init__(self, adapters):
        from types import MappingProxyType
        for (member, project), adapter in adapters.items():
            if (type(member) is not int or member <= 0 or type(project) is not int or project <= 0
                    or adapter.member_id != member or adapter.project_id != project):
                raise ProviderUnavailable('Workspace mapping is inconsistent')
        self.adapters = MappingProxyType(dict(adapters))

    def adapter(self, member, project):
        value = self.adapters.get((member, project))
        if value is None:raise ProviderUnavailable('Workspace mapping is not configured')
        return value

    def initialize_from_snapshot(self, target, base, basis_id, files):
        return self.adapter(target.student_id, target.project_id).initialize_from_snapshot(target, base, basis_id, files)

    def initialize_for_request(self,target,base,basis_id,files,request_id):
        adapter=self.adapter(target.student_id,target.project_id)
        initialize=getattr(adapter,'initialize_for_request',None)
        return initialize(target,base,basis_id,files,request_id) if initialize else adapter.initialize_from_snapshot(target,base,basis_id,files)

    def verify(self, proof):
        return self.adapter(proof.target.student_id, proof.target.project_id).verify(proof)

    def verify_collection(self, basis, row):
        return self.adapter(basis.member_id, basis.project_id).verify_collection(basis, row)

    def unchanged(self, basis):
        return self.adapter(basis.member_id, basis.project_id).unchanged(basis)

    def lookup_operation(self,operation,target):
        adapter=self.adapter(target.student_id,target.project_id)
        inspect=getattr(adapter,'lookup_operation',None)
        return inspect(operation,target) if inspect else {'state':'UNKNOWN'}


class LinuxWorkspaces:
    def __init__(self, root, member_uid, member_gid, *, member_id, project_id, collection_verifier=None):
        if sys.platform != 'linux' or os.geteuid() == 0 or os.getuid() != os.geteuid():
            raise ProviderUnavailable('Linux non-root workspace manager required')
        if type(member_uid) is not int or member_uid <= 0 or type(member_gid) is not int or member_gid <= 0:
            raise ProviderUnavailable('Workspace identity is not configured')
        if os.geteuid() != member_uid or os.getegid() != member_gid:
            raise ProviderUnavailable('Dedicated member management worker required')
        # rootは管理者設定だけで受け取り、symlinkを含む配置は起動時に拒否する。
        from pathlib import Path
        path = Path(root)
        if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
            raise ProviderUnavailable('Workspace root is unsafe')
        self.root = str(path)
        self.member_uid, self.member_gid = member_uid, member_gid
        self.member_id, self.project_id = member_id, project_id
        self.collection_verifier = collection_verifier
        with self.root_fd():pass

    @contextmanager
    def root_fd(self):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ProviderUnavailable('Workspace root must be manager-owned and private')
            yield fd
        finally:os.close(fd)

    @staticmethod
    def canonical(value):
        if str(UUID(value)) != value:raise ProviderUnavailable('Invalid workspace identifier')
        return value

    @staticmethod
    def entry_mode(mode):
        # 既存収集器の「所有者一致・group/other書込み禁止」と同じ方針にする。
        # 管理ワーカーは同じUIDでも別コンテナ/PID namespaceに隔離し、証拠rootを
        # code-serverへmountしない。共通groupの書込み権限で境界を弱めない。
        if mode not in ('100644','100755'):raise ProviderUnavailable('Unsupported workspace mode')
        return 0o700 if mode == '100755' else 0o600

    def initialize_from_snapshot(self, target, base, basis_id, files):
        self.canonical(basis_id)
        # DB IDとOS UIDを混同せず、管理側の固定対応を別々に照合する。
        if target.student_id != self.member_id or target.project_id != self.project_id:
            raise ProviderUnavailable('Workspace member or project does not match')
        workspace_id = str(uuid4())
        proof = WorkspaceInitialization(basis_id, target, base, workspace_id, 1)
        if len(files) > 1000 or sum(len(body) for _, body in files.values()) > 10 * 1024 * 1024:
            raise ProviderUnavailable('Workspace snapshot exceeds limits')
        manifest = {}
        with self.root_fd() as root:
            # mkdir失敗時は既存領域を再利用しない。部分作成も自動削除しない。
            os.mkdir('w-' + workspace_id, 0o700, dir_fd=root)
            work = os.open('w-' + workspace_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                for path, (mode, body) in sorted(files.items()):
                    valid_path(path)
                    if mode not in ('100644', '100755') or len(body) > 1024 * 1024:
                        raise ProviderUnavailable('Unsupported workspace entry')
                    parent = os.dup(work)
                    try:
                        parts = path.split('/')
                        for part in parts[:-1]:
                            try:os.mkdir(part, 0o700, dir_fd=parent)
                            except FileExistsError:pass
                            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                            os.fchmod(child, 0o700)
                            os.close(parent);parent = child
                        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(body);stream.flush()
                            os.fchmod(stream.fileno(), self.entry_mode(mode))
                            os.fsync(stream.fileno())
                        os.fsync(parent)
                        manifest[path] = [mode, hashlib.sha256(body).hexdigest()]
                    finally:os.close(parent)
                os.fchmod(work, 0o700);os.fsync(work)
            finally:os.close(work)
            record = json.dumps({'proof': asdict(proof), 'files': manifest}, sort_keys=True).encode()
            fd = os.open('m-' + workspace_id + '.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(record);stream.flush();os.fsync(stream.fileno())
            os.fsync(root)
        return proof

    def record(self, workspace_id):
        self.canonical(workspace_id)
        with self.root_fd() as root:
            fd = os.open('m-' + workspace_id + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ProviderUnavailable('Workspace evidence is unsafe')
                raw = stream.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:raise ProviderUnavailable('Workspace evidence exceeds limit')
                return json.loads(raw)

    def verify(self, proof):
        if self.record(proof.workspace_id)['proof'] != asdict(proof):return False
        with self.root_fd() as root:
            work = os.open('w-' + proof.workspace_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                info = os.fstat(work)
                return (info.st_uid == os.geteuid() and info.st_gid == self.member_gid
                        and info.st_dev == os.fstat(root).st_dev and stat.S_IMODE(info.st_mode) == 0o700)
            finally:os.close(work)

    def verify_collection(self, basis, row):
        # ファイル初期化の証明をセッション認証の代用にしない。
        return self.collection_verifier is not None and self.collection_verifier(basis, row) is True

    def unchanged(self, basis):
        record = self.record(basis.workspace_id)
        if record['proof']['basis_id'] != basis.id or record['proof']['base_commit'] != basis.base_commit:
            return False
        with self.root_fd() as root:
            work = os.open('w-' + basis.workspace_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            try:
                first = self.scan(work)
                second = self.scan(work)
                return first == second == record['files']
            finally:os.close(work)

    def scan(self, root):
        result = {};total = scanned = 0;device = os.fstat(root).st_dev
        deadline = time.monotonic() + 30
        def mount_id(fd):
            # st_devだけでは同一FSのbind mountを検出できない。FD自身のmount IDも
            # 比較し、proc情報を読めない環境では安全と推測せず拒否する。
            with open('/proc/self/fdinfo/' + str(fd), encoding='ascii') as stream:
                values = [line.split(':', 1)[1].strip() for line in stream if line.startswith('mnt_id:')]
            if len(values) != 1 or not values[0].isdigit():raise ProviderUnavailable('Mount identity unavailable')
            return int(values[0])
        mount = mount_id(root)
        def walk(parent, prefix='', depth=0):
            nonlocal total, scanned
            if depth > 20:raise ProviderUnavailable('Workspace depth exceeded')
            names=[]
            # 空ディレクトリも総件数に含め、無制限のlistdirでメモリを消費しない。
            with os.scandir(parent) as entries:
                for entry in entries:
                    scanned+=1
                    if scanned>10000 or time.monotonic()>deadline:
                        raise ProviderUnavailable('Workspace inspection limit exceeded')
                    names.append(entry.name)
            for name in sorted(names):
                path = prefix + name;valid_path(path)
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    before = os.fstat(fd)
                    if (before.st_dev != device or mount_id(fd) != mount or before.st_uid != self.member_uid
                            or before.st_mode & 0o022):
                        raise ProviderUnavailable('Workspace ownership or mount changed')
                    if stat.S_ISDIR(before.st_mode):walk(fd, path + '/', depth + 1)
                    elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                        if before.st_size > 1024 * 1024 or len(result) >= 1000:
                            raise ProviderUnavailable('Workspace file limit exceeded')
                        body = bytearray()
                        while chunk := os.read(fd, min(65536, 1024 * 1024 + 1 - len(body))):
                            if time.monotonic()>deadline:raise ProviderUnavailable('Workspace inspection deadline exceeded')
                            body.extend(chunk)
                        if len(body) > 1024 * 1024:raise ProviderUnavailable('Workspace file grew beyond limit')
                        after = os.fstat(fd)
                        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                            raise ProviderUnavailable('Workspace changed during inspection')
                        total += len(body)
                        if total > 10 * 1024 * 1024:raise ProviderUnavailable('Workspace size exceeded')
                        result[path] = ['100755' if before.st_mode & 0o111 else '100644', hashlib.sha256(body).hexdigest()]
                    else:raise ProviderUnavailable('Workspace links or special files are forbidden')
                finally:os.close(fd)
        walk(root)
        return result
