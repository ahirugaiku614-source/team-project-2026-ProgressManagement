"""第8段階の設計資料だけを静的に検査する。実行基盤を検査・作成しない。

ネットワーク、Docker、libvirt、DNS、証明書、DB、Firebaseへアクセスしない。
申告された設定と証跡の形式が整合しても、実機の安全性・配備許可には変換しない。
入力内容や内部パスをエラーへ反射せず、固定コードだけを出力する。
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 65536
# 5758a62時点の第7段階ソースを固定する。CRLF/LF差だけを正規化する。
# 将来の実行ゲート変更は、この基準も含めて別PRで明示的にレビューする必要がある。
RUNTIME_HASHES = {
    "editor_contracts.py": "5cdf36e61f7d5a037ce5dc1f56747237c6ef24bb93fe8b2c3c730c01de91993d",
    "editor_api.py": "c7de78729aec227000a7d8693f54b92ca26be4803a49cace96c2d4ddc41fe24f",
    "editor_access.py": "748f5244083645f80b8a99d09f695eee392e6befefb2a0772c7755cd2de9cf5c",
}
CHECK_IDS = (
    "dedicated_host", "kvm_available", "hypervisor_confinement", "vm_binding", "storage_boundary",
    "ipv4_ipv6_egress", "metadata_block", "no_secrets_sockets", "gateway_only", "dns_tls",
    "firebase_revocation", "host_cookie_binding", "csrf_origin", "ws_upgrade", "ws_revocation",
    "worker_mtls_nonce", "lifecycle_fencing", "data_retention", "submission_snapshot", "backup_restore",
    "resource_quotas", "logs_no_secrets", "legacy_regression",
)
CHANNELS = {
    ("browser", "gateway", "https", 443),
    ("application", "manager", "mtls", 8443),
    ("gateway", "authorizer", "mtls", 8443),
    ("manager", "authorizer", "mtls", 8443),
    ("manager", "hypervisor", "mtls", 16514),
    ("gateway", "editor_vm", "mtls", 8443),
}


class InvalidDesign(Exception):
    """入力値を含めない、表示可能な検証コードを保持する。"""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparseの既定エラーは不明な引数の値も反射するため、固定コードへ変換する。
        raise InvalidDesign("invalid_arguments")


def bounded_read(path):
    # FIFO等で停止しない。シンボリックリンクも通常の入力形式にしない。
    path = Path(path)
    if path.is_symlink():
        raise InvalidDesign("unsafe_input_file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES or info.st_nlink != 1:
            raise InvalidDesign("unsafe_input_file")
        data = bytearray()
        while chunk := os.read(fd, min(8192, MAX_BYTES + 1 - len(data))):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise InvalidDesign("input_too_large")
        return bytes(data)
    finally:
        os.close(fd)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InvalidDesign("duplicate_json_key")
        value[key] = item
    return value


def load_json(path):
    try:
        return json.loads(bounded_read(path).decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise InvalidDesign("invalid_json") from error


def reject_constant(value):
    """標準JSONではないNaN/Infinityを、比較結果のすり抜けに使わせない。"""
    raise InvalidDesign("invalid_json_number")


def shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise InvalidDesign("unknown_or_missing_field")


def exact(value, expected, code):
    # True == 1のようなPythonの比較による紛れ込みを許可しない。
    if type(value) is not type(expected) or value != expected:
        raise InvalidDesign(code)


def validate_plan(plan):
    shape(plan, ("version", "mode", "isolation", "launch_enabled", "infrastructure_prepared", "guest", "network", "https", "storage", "capacity"))
    for key, expected in (("version", 1), ("mode", "DESIGN_ONLY"), ("isolation", "VM_PER_USER_PROJECT"),
                          ("launch_enabled", False), ("infrastructure_prepared", False)):
        exact(plan[key], expected, "design_only_required")
    fixed = {
        "guest": {"os": "LINUX", "non_root": True, "dedicated_kernel": True, "host_shares": False,
                  "docker_socket": False, "app_credentials": False, "public_ports": False},
        "storage": {"dedicated_data_disk": True, "delete_on_stop": False, "legacy_access": False,
                    "migration": "NOT_IMPLEMENTED", "submission_bridge": "NOT_IMPLEMENTED"},
        "https": {"app_origin": "https://app.example.com", "editor_suffix": "editors.example.net",
                  "dns_address_example": "192.0.2.10", "tls": "PLANNED_NOT_ISSUED", "gateway": "NOT_IMPLEMENTED",
                  "http_auth_each_request": True, "ws_auth_before_accept": True,
                  "ws_recheck_seconds": 15, "session_max_seconds": 300, "cookie": "__Host-editor",
                  "cookie_secure": True, "cookie_http_only": True, "cookie_same_site": "Lax",
                  "cookie_domain": None, "cookie_path": "/", "strip_upstream_credentials": True},
    }
    for section, expected_values in fixed.items():
        shape(plan[section], expected_values)
        for key, expected in expected_values.items():
            exact(plan[section][key], expected, "unsafe_design_declaration")
    # 予約ドメイン・文書用IPだけを許可する。実DNSや実接続先を記述する配備設定ではない。
    network = plan["network"]
    shape(network, ("enforcement", "guest_new_egress", "ipv6", "metadata", "service_channels"))
    for key, value in (("enforcement", "OUTSIDE_GUEST"), ("guest_new_egress", "DENY"),
                       ("ipv6", "DENY_UNTIL_VERIFIED"), ("metadata", "DENY")):
        exact(network[key], value, "unsafe_network_design")
    channels = network["service_channels"]
    if not isinstance(channels, list) or len(channels) != len(CHANNELS):
        raise InvalidDesign("service_channels_mismatch")
    actual = set()
    for item in channels:
        shape(item, ("source", "target", "protocol", "port"))
        if any(type(item[k]) is not str for k in ("source", "target", "protocol")) or type(item["port"]) is not int:
            raise InvalidDesign("service_channels_mismatch")
        actual.add((item["source"], item["target"], item["protocol"], item["port"]))
    if actual != CHANNELS:
        raise InvalidDesign("service_channels_mismatch")
    capacity = plan["capacity"]
    keys = ("host_vcpu", "reserved_vcpu", "guest_vcpu", "host_memory_mib", "reserved_memory_mib", "guest_memory_mib",
            "host_disk_gib", "reserved_disk_gib", "guest_root_gib", "guest_data_gib", "backup_budget_gib", "max_running", "max_retained")
    shape(capacity, keys)
    if any(type(capacity[k]) is not int or not 0 < capacity[k] <= 1048576 for k in keys) or capacity["max_running"] > 16:
        raise InvalidDesign("invalid_capacity")
    n, retained = capacity["max_running"], capacity["max_retained"]
    if retained < n:
        raise InvalidDesign("invalid_capacity")
    # 停止中もデータを保持するため、ディスク予算は起動数でなく保持総数を使う。
    if (capacity["reserved_vcpu"] + n * capacity["guest_vcpu"] > capacity["host_vcpu"]
            or capacity["reserved_memory_mib"] + n * capacity["guest_memory_mib"] > capacity["host_memory_mib"]
            or capacity["reserved_disk_gib"] + capacity["backup_budget_gib"]
            + retained * (capacity["guest_root_gib"] + capacity["guest_data_gib"]) > capacity["host_disk_gib"]):
        raise InvalidDesign("capacity_overcommitted")


def validate_evidence(evidence):
    shape(evidence, ("version", "environment", "checks"))
    exact(evidence["version"], 1, "invalid_evidence")
    exact(evidence["environment"], "NOT_PREPARED", "design_only_required")
    shape(evidence["checks"], CHECK_IDS)
    counts = {"NOT_RUN": 0, "PASS": 0, "FAIL": 0}
    for check in evidence["checks"].values():
        shape(check, ("status", "evidence_id"))
        if type(check["status"]) is not str or check["status"] not in counts:
            raise InvalidDesign("invalid_evidence")
        ref = check["evidence_id"]
        if ref is not None:
            if type(ref) is not str or not re.fullmatch(r"TEST-[A-Z0-9_-]{1,64}", ref):
                raise InvalidDesign("invalid_evidence_reference")
        if (check["status"] == "PASS" and ref is None) or (check["status"] == "NOT_RUN" and ref is not None):
            raise InvalidDesign("invalid_evidence_reference")
        counts[check["status"]] += 1
    return counts


def check_runtime_source(root):
    # importも実行もしない。SDK初期化・既存DB・実行ゲートの評価に副作用を持ち込まない。
    for filename, expected in RUNTIME_HASHES.items():
        content = bounded_read(Path(root) / "backend" / "app" / filename).replace(b"\r\n", b"\n")
        if hashlib.sha256(content).hexdigest() != expected:
            raise InvalidDesign("stage7_source_changed")


def evaluate(plan_path, evidence_path, root=ROOT):
    result = {"status": "invalid_design", "editor_launch_enabled": False, "deployment_allowed": False,
              "real_infrastructure_verified": False, "runtime_source_unchanged": False, "errors": []}
    try:
        check_runtime_source(root)
        result["runtime_source_unchanged"] = True
        validate_plan(load_json(plan_path))
        counts = validate_evidence(load_json(evidence_path))
        result["evidence_claims"] = counts
        result["status"] = "evidence_failed" if counts["FAIL"] else "design_consistent"
        # PASSは利用者の申告であって実測・署名検証ではない。全件PASSでも三つの許可値はFalse。
    except InvalidDesign as error:
        result["errors"] = [str(error)]
    except (OSError, TypeError, KeyError, ValueError):
        result["errors"] = ["input_unavailable"]
    return result


def main(argv=None):
    parser = SafeParser(description="設計JSONの静的検査のみ。実行・配備・実機確認は行いません。")
    parser.add_argument("--plan", default=ROOT / "docs/editor-foundation/plan.example.json", type=Path)
    parser.add_argument("--evidence", default=ROOT / "docs/editor-foundation/evidence.example.json", type=Path)
    try:
        args = parser.parse_args(argv)
        report = evaluate(args.plan, args.evidence)
    except InvalidDesign:
        report = {"status": "invalid_design", "editor_launch_enabled": False, "deployment_allowed": False,
                  "real_infrastructure_verified": False, "runtime_source_unchanged": False, "errors": ["invalid_arguments"]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "design_consistent" else 2


if __name__ == "__main__":
    raise SystemExit(main())
