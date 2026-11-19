"""Docker Engine向け固定設定と所有者照合。実SDK/CLI/socket実装は追加しない。"""

from copy import deepcopy
import re
from typing import Protocol

from execution_worker.contracts import Observation, Unavailable, canonical, fingerprint, full_id, image_id


class EnginePort(Protocol):
    """将来の限定Docker接続境界。各操作は期限内に戻り、自動retry/pullしない契約。"""

    def prerequisites(self, image: str) -> dict:
        """Linux/rootful/runsc/cgroup/image検証結果を返す。設定名だけで証明しない。"""
        ...

    def create(self, name: str, spec: dict) -> str:
        """固定設定だけで新規作成し、完全IDを返す。失敗時に再作成しない。"""
        ...

    def inspect(self, container_id: str) -> Observation | None:
        """完全IDを照合して返す。Noneは確定した404だけで、通信障害と混同しない。"""
        ...

    def attach(self, container_id: str):
        """start前に非TTY入出力を接続する。管理socketをguestへ渡さない。"""
        ...

    def start(self, container_id: str):
        """確認済み本人コンテナを開始する。hostで学生コードを評価しない。"""
        ...

    def send(self, container_id: str, source: bytes):
        """stdinへsourceを送って入力側を閉じる。shell文字列へ埋め込まない。"""
        ...

    def read(self, container_id: str, timeout_ms: int, maximum: int):
        """期限とraw byte上限付きのFrame、または未到着を示すNoneを返す。"""
        ...

    def kill(self, container_id: str):
        """現在所有確認済みの完全IDだけを停止する。既存の他resourceは対象外。"""
        ...

    def remove(self, container_id: str):
        """停止済みの完全IDだけを削除する。force・volume削除・pruneは使わない。"""
        ...


class DisconnectedEngine:
    """実Engine未接続の既定境界。環境変数やDocker default contextを自動参照しない。"""

    def prerequisites(self, image):
        """設定変更・接続・sudoをせず、常に拒否する。"""
        raise Unavailable()


def specification(image, labels):
    """Docker create相当の固定allowlistを生成する。bind/volume/device/公開portは持たない。"""
    if not image_id(image):
        raise Unavailable()
    return {
        "Image": image, "User": "1000:1000", "WorkingDir": "/work",
        "Entrypoint": ["python3"], "Cmd": ["-I", "-B", "-u", "-"],
        "Env": ["PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/work", "TMPDIR=/tmp"], "Labels": deepcopy(labels),
        "AttachStdin": True, "AttachStdout": True, "AttachStderr": True,
        "OpenStdin": True, "StdinOnce": True, "Tty": False, "NetworkDisabled": True,
        "HostConfig": {
            "Runtime": "runsc", "NetworkMode": "none", "ReadonlyRootfs": True,
            "Privileged": False, "CapDrop": ["ALL"], "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"], "NanoCpus": 500000000,
            "Memory": 128 * 1024**2, "MemorySwap": 128 * 1024**2, "PidsLimit": 32,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m,mode=1777",
                      "/work": "rw,noexec,nosuid,nodev,size=16m,mode=700,uid=1000,gid=1000"},
            "ShmSize": 8 * 1024**2, "Binds": [], "Mounts": [], "Devices": [],
            "PortBindings": {}, "PublishAllPorts": False, "AutoRemove": False,
            "RestartPolicy": {"Name": "no"}, "LogConfig": {"Type": "none", "Config": {}},
        },
    }


class Driver:
    """全操作前の完全ID・固定spec照合を担う。実Docker接続はportの未実装機能である。"""

    def __init__(self, image=None, port=None, daemon_id=None):
        """imageも実portも既定で未設定。未確認imageを自動pull/buildしない。"""
        self.image = image
        self.daemon_id = daemon_id
        self.port = port if port is not None else DisconnectedEngine()

    def ready(self):
        """runsc以外や未確認制限を拒否する。rootlessを検証済みとは扱わない。"""
        self.profile()
        expected = {"linux": True, "rootful": True, "runsc_verified": True, "cgroup_v2": True,
                    "cpu_memory_pids_swap": True, "image_id": self.image, "image_no_volumes": True,
                    "image_environment_clean": True, "image_architecture_verified": True,
                    "daemon_id": self.daemon_id}
        if canonical(self.port.prerequisites(self.image)) != canonical(expected):
            raise Unavailable()

    def profile(self):
        """static設定を検査し、通常実行/復旧の両方で同じdaemon/imageへ台帳を束縛する。"""
        if not image_id(self.image) or type(self.daemon_id) is not str or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", self.daemon_id):
            raise Unavailable()
        return fingerprint({"daemon": self.daemon_id, "policy": specification(self.image, {})})

    def owned(self, cid, spec, absent=False):
        """完全specにはowner/実行ID/nonce/generationが入る。名前やラベルだけで操作しない。"""
        if not full_id(cid):
            raise Unavailable()
        observed = self.port.inspect(cid)
        if observed is None and absent:
            return None
        if (not isinstance(observed, Observation) or observed.container_id != cid
                or canonical(observed.spec) != canonical(spec) or type(observed.running) is not bool
                or (observed.exit_code is not None and (type(observed.exit_code) is not int or not -(2**31) <= observed.exit_code < 2**31))):
            raise Unavailable()
        return observed

    def stop(self, cid, spec):
        """所有者確認後だけkillし、停止を確認する。失敗/不明は削除へ進まない。"""
        observed = self.owned(cid, spec)
        if observed.running:
            self.port.kill(cid)
        observed = self.owned(cid, spec)
        if observed.running or observed.exit_code is None:
            raise Unavailable()
        return observed.exit_code

    def cleanup(self, cid, spec):
        """停止済みだけremoveし、確定404まで確認する。未確認で予約枠を戻さない。"""
        observed = self.owned(cid, spec, absent=True)
        if observed is None:
            return
        if observed.running:
            raise Unavailable()
        self.port.remove(cid)
        if self.owned(cid, spec, absent=True) is not None:
            raise Unavailable()
