"""Linuxの既知ファイルとメタデータだけを読む。外部コマンドやsocketは使用しない。"""

import hashlib
import os
from pathlib import PurePosixPath
import re
import stat


class Unavailable(Exception):
    """読取り失敗を固定例外にする。元のパス・ファイル内容をログへ反射しない。"""


def components(path):
    """絶対Linuxパスを分解する。親移動・NUL・相対指定を許可しない。"""
    if (type(path) is not str or not path.startswith("/") or "\x00" in path
            or any(part in (".", "..", "") for part in path.split("/")[1:])):
        raise Unavailable()
    return PurePosixPath(path).parts[1:]


def open_readonly(path, directory=False):
    """各親directoryをFD経由で開き、途中のsymlinkも辿らない。create/writeはしない。"""
    parts = components(path)
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            os.close(parent)
            parent = child
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if (directory and not stat.S_ISDIR(info.st_mode)) or (not directory and not stat.S_ISREG(info.st_mode)):
            raise Unavailable()
        return os.open(parts[-1], flags, dir_fd=parent)
    finally:
        os.close(parent)


def read_text(path, maximum=1024 * 1024):
    """サイズ上限つきで通常/proc/sysfsファイルを読む。FIFO/device/socketは読まない。"""
    fd = open_readonly(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or info.st_size > maximum:
            raise Unavailable()
        chunks, remaining = [], maximum + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum:
            raise Unavailable()
        return content.decode("utf-8", errors="strict")
    finally:
        os.close(fd)


def mount_records(text):
    """mountinfoからfs種別・mount先・optionsだけを抽出する。source等の秘密情報は返さない。"""
    result = []
    for line in text.splitlines():
        left, right = line.split(" - ", 1)
        fields, tail = left.split(), right.split()
        if len(fields) < 6 or len(tail) < 3:
            raise Unavailable()
        # escapeを誤解して別mountを選ばない。複雑な名前は安全側に未対応とする。
        if "\\" in fields[3] or "\\" in fields[4]:
            raise Unavailable()
        result.append((fields[3], fields[4], tail[0], set(fields[5].split(",")) | set(tail[2].split(","))))
    return result


def local_mount(text, path):
    """指定先に最も近いmountを調べ、remote/autofs等を開く前に拒否する。"""
    components(path)
    candidates = [(len(point), kind, options) for _, point, kind, options in mount_records(text)
                  if point == "/" or path == point or path.startswith(point.rstrip("/") + "/")]
    if not candidates:
        raise Unavailable()
    longest = max(item[0] for item in candidates)
    closest = [item for item in candidates if item[0] == longest]
    if len(closest) != 1:
        raise Unavailable()  # 重なったmountの可視順序を推測しない。
    _, kind, options = closest[0]
    if kind not in ("ext4", "xfs", "btrfs"):
        raise Unavailable()
    return kind, options


class LocalProbe:
    """専用Linuxホストで明示選択されたときだけ使う、ファイル読取りの境界。"""

    def system(self):
        """unameと現在UIDをsyscallで取得する。hostname、NSS、環境変数は調査しない。"""
        if not hasattr(os, "uname") or not hasattr(os, "getuid"):
            return {"system": "UNSUPPORTED"}
        info = os.uname()
        return {"system": info.sysname, "machine": info.machine, "kernel": info.release, "uid": os.getuid()}

    def text(self, key):
        """固定allowlistだけを読む。passwdは本人のsubuid照合用で出力しない。"""
        files = {
            "os_release": "/usr/lib/os-release", "os_release_fallback": "/etc/os-release",
            "passwd": "/etc/passwd", "subuid": "/etc/subuid", "subgid": "/etc/subgid",
            "max_userns": "/proc/sys/user/max_user_namespaces",
            "max_netns": "/proc/sys/user/max_net_namespaces",
            "unprivileged_userns": "/proc/sys/kernel/unprivileged_userns_clone",
            "apparmor_userns": "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
            "cgroup_membership": f"/proc/{os.getpid()}/cgroup",
            "mountinfo": f"/proc/{os.getpid()}/mountinfo",
            "root_controllers": "/sys/fs/cgroup/cgroup.controllers",
        }
        if key in files:
            return read_text(files[key])
        # 現在所属cgroupの観測に限定し、CLIから任意パスを受け付けない。
        if key.startswith("current_"):
            suffix = {"current_controllers": "cgroup.controllers", "current_subtree": "cgroup.subtree_control",
                      "current_cpu": "cpu.max", "current_memory": "memory.max",
                      "current_pids": "pids.max", "current_swap": "memory.swap.max"}.get(key)
            records = mount_records(self.text("mountinfo"))
            if suffix is None or not any(root == "/" and point == "/sys/fs/cgroup" and kind == "cgroup2" for root, point, kind, _ in records):
                raise Unavailable()
            lines = self.text("cgroup_membership").splitlines()
            if len(lines) != 1 or not lines[0].startswith("0::/"):
                raise Unavailable()
            group = lines[0][3:]
            if group != "/":
                components(group)
            return read_text("/sys/fs/cgroup" + (group if group != "/" else "") + "/" + suffix)
        raise Unavailable()

    def namespace(self, kind):
        """procの既知namespace linkをreadlinkだけで観測する。unshare/setnsはしない。"""
        if kind not in ("user", "net"):
            raise Unavailable()
        target = os.readlink(f"/proc/{os.getpid()}/ns/{kind}")
        return re.fullmatch(rf"{kind}:\[\d+\]", target) is not None

    def mapping_helper(self, kind):
        """UID/GID helperの配置と属性だけを観測する。setuid helperを呼出さない。"""
        paths = {"uid": "/usr/bin/newuidmap", "gid": "/usr/bin/newgidmap"}
        local_mount(self.text("mountinfo"), paths[kind])
        fd = open_readonly(paths[kind])
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022:
                raise Unavailable()
            return {"executable_bit": bool(info.st_mode & 0o111), "setuid_bit": bool(info.st_mode & stat.S_ISUID),
                    "root_owned": info.st_uid == 0}
        finally:
            os.close(fd)

    def binary(self, path):
        """runsc候補のELF形式・実行bit・SHA256を読む。--versionさえ実行しない。"""
        local_mount(self.text("mountinfo"), path)
        fd = open_readonly(path)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 256 * 1024**2
                    or before.st_size < 64 or not before.st_mode & 0o111 or before.st_mode & 0o6022):
                raise Unavailable()
            header = os.read(fd, 64)
            if header[:6] != b"\x7fELF\x02\x01":
                raise Unavailable()
            machine = int.from_bytes(header[18:20], "little")
            architecture = {62: "amd64", 183: "arm64"}.get(machine, "unsupported")
            digest = hashlib.sha256(header)
            count = len(header)
            while count <= 256 * 1024**2:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                count += len(chunk)
            after = os.fstat(fd)
            if count != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise Unavailable()
            return {"architecture": architecture, "sha256": digest.hexdigest(), "executable_bit": True}
        finally:
            os.close(fd)

    def storage(self, path):
        """既知local FSだけで容量を観測する。remote FSやquotaへの書込みprobeはしない。"""
        kind, options = local_mount(self.text("mountinfo"), path)
        fd = open_readonly(path, directory=True)
        try:
            usage = os.fstatvfs(fd)
            return {"filesystem": kind, "total_bytes": usage.f_blocks * usage.f_frsize,
                    "available_bytes": usage.f_bavail * usage.f_frsize, "available_inodes": usage.f_favail,
                    "quota_mount_option": bool(options & {"usrquota", "grpquota", "prjquota", "pquota", "uquota", "gquota"})}
        finally:
            os.close(fd)
