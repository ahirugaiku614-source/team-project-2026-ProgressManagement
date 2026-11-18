"""rootless/runscの安全な設定案を生成・厳密照合する。Docker/OCIを実行する機能はない。"""

from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import re
from uuid import NAMESPACE_URL, uuid5

from app.editor_contracts import EditorBinding
from minimal_editor_lab.contracts import Budget, Profile


class Denied(Exception):
    """入力値・依存先の内部情報を表示しない、gVisor検証用の固定拒否。"""

    def __init__(self):
        super().__init__("gvisor_lab_denied")


def safe(function):
    """検査・依存モックの例外を秘匿する。tokenやパスをエラー本文に反射しない。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            raise Denied() from None
    return wrapped


def encode(value):
    """boolとintの等価比較を避けるため、設定を型の残る正規JSONとして比較する。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    """世代・完了証跡へ束縛する設定fingerprint。秘密値を保存する用途には使わない。"""
    return hashlib.sha256(encode(value).encode()).hexdigest()


def identities(binding):
    """gVisor専用名前空間のsandbox/volume IDを確定する。KVMのvm_idを流用しない。"""
    if not isinstance(binding, EditorBinding):
        raise Denied()
    return tuple(str(uuid5(NAMESPACE_URL, f"team-progress/gvisor/{kind}/v1/{binding.project_id}/{binding.user_id}"))
                 for kind in ("sandbox", "volume"))


def digest_valid(value):
    """未確定・latest・全ゼロをイメージ/ランタイム検証済みdigestとして受理しない。"""
    return type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None and value != "sha256:" + "0" * 64


@dataclass(frozen=True)
class Capabilities:
    """将来の専用検証器が認証して発行すべき短期証跡。現段階はモックだけが生成する。

    自己申告JSONやruntimeの登録名だけを実証跡にしない。ホストprobe/署名検証は未実装。
    storage_limit_gibは既存の未使用予約も含めた総commit上限であり、単なる空きbyteではない。
    """

    binding: EditorBinding
    sampled_ms: int
    runtime: str
    platform: str
    architecture: str
    runtime_digest: str
    image_digest: str
    caller_uid: int
    rootless_chain: bool
    caller_userns: bool
    cgroup_v2_delegated: bool
    cpu_memory_pids_enforced: bool
    disk_quota_enforced: bool
    network_namespace_isolated: bool
    storage_healthy: bool
    storage_limit_gib: int

    def validate(self, binding, now):
        """runsc以外・rootful・未確認制限・古い観測を拒否する。代替runtimeは選ばない。"""
        if (self.binding != binding or self.runtime != "runsc" or self.platform != "systrap"
                or self.architecture not in ("amd64", "arm64")
                or not digest_valid(self.runtime_digest) or not digest_valid(self.image_digest)
                or type(self.caller_uid) is not int or self.caller_uid <= 0
                or type(self.sampled_ms) is not int or not 0 <= now - self.sampled_ms <= 5000
                or type(self.storage_limit_gib) is not int or self.storage_limit_gib < 0
                or any(value is not True for value in (self.rootless_chain, self.caller_userns, self.cgroup_v2_delegated,
                           self.cpu_memory_pids_enforced, self.disk_quota_enforced, self.network_namespace_isolated, self.storage_healthy))):
            raise Denied()


def plan_for(binding, profile, image_digest):
    """rootless engine向けの設定案を固定生成する。logical volumeは実パスに解決しない。

    これはDocker SDK/OCIそのもののリクエストではなく、未配備の限定契約である。
    将来のprivileged resolverが本人volume・quota・UID mappingを検証する必要がある。
    """
    if not isinstance(profile, Profile) or not digest_valid(image_digest):
        raise Denied()
    sandbox_id, volume_id = identities(binding)
    return {
        "kind": "GVISOR_LAB_PLAN", "runtime": "runsc", "platform": "systrap",
        "rootless_mode": "CALLER_CONFIGURED_USERNS", "fallback": False,
        "sandbox_id": sandbox_id, "image_digest": image_digest,
        "user": "1000:1000", "privileged": False, "cap_drop": ["ALL"],
        "no_new_privileges": True, "read_only_root": True,
        "network": "none", "publish": [], "devices": [], "host_namespaces": [],
        "cpu_millis": profile.cpu * 1000, "memory_bytes": profile.memory_mib * 1024**2,
        "swap_bytes": 0, "pids_limit": 256,
        "writable_layer_gib": profile.root_gib, "data_quota_gib": profile.data_gib,
        "mounts": [
            {"destination": "/workspace", "source": "OWNER_DATA:" + volume_id, "read_only": False,
             "options": ["nosuid", "nodev"]},
            {"destination": "/run/editor-config", "source": "OWNER_CONFIG:" + sandbox_id, "read_only": True,
             "options": ["nosuid", "nodev", "noexec"]},
        ],
        "tmpfs": {"/tmp": {"mib": 64, "options": ["nosuid", "nodev", "noexec"]},
                  "/home/coder": {"mib": 32, "options": ["nosuid", "nodev", "noexec"]}},
        "entrypoint": ["python3", "/opt/gvisor-lab/disabled_entrypoint.py"],
        "environment": {"HOME": "/home/coder", "XDG_DATA_HOME": "/workspace/.editor/data",
                        "XDG_CONFIG_HOME": "/workspace/.editor/config", "TMPDIR": "/tmp"},
        "launch_enabled": False,
    }


@safe
def validate_plan(plan, binding, profile, image_digest):
    """allowlistから一文字でも逸脱する設定を拒否する。未知mount/環境変数/CLI引数も通さない。"""
    if encode(plan) != encode(plan_for(binding, profile, image_digest)):
        raise Denied()
    return fingerprint(plan)


class DisabledExecutor:
    """実行アダプターの既定値。Docker socket/CLI/runsc/ネットワークへ接続する実装はない。"""

    def execute(self, ticket, plan):
        """どの要求も固定拒否する。失敗時にrunc等へ再試行するコードは存在しない。"""
        raise Denied()
