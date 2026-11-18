"""読取り結果を安全な事前調査レポートへ変換する。合格証明やCapabilitiesは発行しない。"""

import argparse
import json
import re

from gvisor_host_preflight.probe import LocalProbe, mount_records


RUNTIME_CHECKS = (
    "runsc_version", "runtime_bundle_integrity", "runtime_no_fallback", "image_provenance",
    "systrap_execution", "rootless_process_chain", "cgroup_delegation",
    "cpu_enforcement", "memory_enforcement", "pids_enforcement", "disk_quota_enforcement",
    "user_project_file_isolation", "host_secret_isolation", "network_isolation", "metadata_block",
    "toolchain", "data_retention", "backup_restore", "crash_recovery", "browser_connection",
)


def report(source="NOT_RUN"):
    """すべての実機試験を未実施とし、観測値で実行ゲートを開けない固定枠を作る。"""
    return {"schema_version": 1, "source": source, "collection": "NOT_RUN", "readiness": "BLOCKED",
            "launch_enabled": False, "deployment_allowed": False, "connectable": False,
            "facts": {}, "runtime_checks": {key: "NOT_RUN" for key in RUNTIME_CHECKS}}


def fact(status="UNKNOWN", **values):
    """静的観測と不明・不適合を区別する。OBSERVEDは実機試験のPASSではない。"""
    return {"status": status, **values}


def attempt(function):
    """権限不足・欠測・解析失敗はUNKNOWN。例外本文やファイルのraw内容を出力しない。"""
    try:
        return function()
    except Exception:
        return fact()


def number(text):
    """sysctl等の整数を厳密解析する。欠測・負数・極端な値は推測で補わない。"""
    if type(text) is not str or not re.fullmatch(r"\d{1,12}\s*", text):
        raise ValueError()
    return int(text)


def subordinate(text, passwd, uid):
    """本人のsubuid/subgid確保数と全range重複を検査する。ユーザー名・range自体は返さない。"""
    names = []
    for line in passwd.splitlines():
        fields = line.split(":")
        if len(fields) != 7:
            raise ValueError()
        if number(fields[2]) == uid:
            names.append(fields[0])
    if len(names) != 1:
        return fact()  # NSSへ照会しない。ローカルpasswdにない利用者はUNKNOWN。
    ranges, own = [], []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        owner, first, count = line.split(":")
        first, count = number(first), number(count)
        if count == 0 or first == 0 or first + count > 2**32:
            return fact("FAIL", reason="invalid_subordinate_range")
        ranges.append((first, first + count))
        if owner in (names[0], str(uid)):
            own.append(count)
    ranges.sort()
    overlap = any(left[1] > right[0] for left, right in zip(ranges, ranges[1:]))
    # NSSや外部directoryとのID衝突までは検査しない。実割当時に管理者が照合する。
    if overlap:
        return fact("FAIL", reason="overlapping_subordinate_ranges")
    if not own or max(own) < 65536:
        return fact("FAIL", reason="insufficient_subordinate_range")
    return fact("OBSERVED", has_contiguous_65536=True, overlap_detected=False, local_id_collision="UNKNOWN")


def collect(probe, runsc_path, storage_path, expected_sha256=None, *, synthetic=False):
    """明示された専用hostを調査する。syntheticはテスト表示専用でCLIから指定させない。"""
    result = report("SYNTHETIC_MOCK" if synthetic else "LOCAL_READ_ONLY")
    system = attempt(probe.system)
    if type(system) is not dict or system.get("system") != "Linux":
        result["facts"]["linux"] = fact("FAIL", reason="unsupported_or_unavailable_os")
        return result
    facts = result["facts"]
    result["collection"] = "COLLECTED"
    machine = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(system.get("machine"))
    facts["architecture"] = fact("OBSERVED" if machine else "FAIL", architecture=machine, runtime_compatibility="NOT_RUN")
    kernel = system.get("kernel", "")
    facts["kernel"] = fact("OBSERVED", release=kernel) if type(kernel) is str and re.fullmatch(r"[A-Za-z0-9._+-]{1,128}", kernel) else fact()
    uid = system.get("uid")
    facts["caller"] = fact("OBSERVED" if type(uid) is int and uid > 0 else "FAIL", non_root=type(uid) is int and uid > 0, entire_process_chain="NOT_RUN")

    def os_release():
        """os-releaseはsourceせずIDとVERSION_IDの小さい文字集合だけを出力する。"""
        try:
            text = probe.text("os_release")
        except Exception:
            text = probe.text("os_release_fallback")
        pairs = {}
        for line in text.splitlines():
            if line.startswith(("ID=", "VERSION_ID=")):
                key, value = line.split("=", 1)
                value = value.strip('"')
                if key in pairs or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
                    raise ValueError()
                pairs[key] = value
        if "ID" not in pairs:
            raise ValueError()
        return fact("OBSERVED", **pairs)
    facts["os"] = attempt(os_release)
    for key in ("max_userns", "max_netns", "unprivileged_userns", "apparmor_userns"):
        def sysctl(key=key):
            """ゼロ上限・無効cloneはFAIL。AppArmorの制約値だけでは可否を断定しない。"""
            value = number(probe.text(key))
            blocked = value == 0 and key != "apparmor_userns"
            return fact("FAIL" if blocked else "OBSERVED", value=value, effective_permission="UNKNOWN")
        facts[key] = attempt(sysctl)
    for kind in ("user", "net"):
        facts[kind + "_namespace"] = attempt(lambda kind=kind: fact("OBSERVED") if probe.namespace(kind) is True else fact())
    for key in ("subuid", "subgid"):
        facts[key] = attempt(lambda key=key: subordinate(probe.text(key), probe.text("passwd"), uid)) if type(uid) is int and uid > 0 else fact()
    for kind in ("uid", "gid"):
        def helper(kind=kind):
            """mapping helperの属性が見えても、file capabilityやLSM許可を推定しない。"""
            observed = probe.mapping_helper(kind)
            if set(observed) != {"executable_bit", "setuid_bit", "root_owned"} or any(type(value) is not bool for value in observed.values()):
                raise ValueError()
            return fact("OBSERVED" if observed["executable_bit"] else "FAIL", **observed, effective_mapping="NOT_RUN")
        facts[kind + "_mapping_helper"] = attempt(helper)

    def cgroup_mount():
        """standard mountのcgroup2という事実だけを確認する。hybrid/別rootは未対応。"""
        records = mount_records(probe.text("mountinfo"))
        standard = [kind for root, point, kind, _ in records if root == "/" and point == "/sys/fs/cgroup"]
        if not standard:
            return fact()
        return fact("OBSERVED" if standard == ["cgroup2"] else "FAIL", unified_v2=standard == ["cgroup2"])
    facts["cgroup_mount"] = attempt(cgroup_mount)

    def controllers(key):
        """controller名の存在だけを記録する。delegationや実上限の強制済みとはしない。"""
        tokens = probe.text(key).split()
        if any(not re.fullmatch(r"[a-z_]+", token) for token in tokens):
            raise ValueError()
        available = sorted(set(tokens) & {"cpu", "memory", "pids"})
        return fact("FAIL" if key == "root_controllers" and len(available) != 3 else "OBSERVED", controllers=available, delegation="UNKNOWN")
    for key in ("root_controllers", "current_controllers", "current_subtree"):
        facts[key] = attempt(lambda key=key: controllers(key))
    for key in ("current_cpu", "current_memory", "current_pids", "current_swap"):
        def limits(key=key):
            """調査プロセス所属cgroupの値を読む。将来sandboxの制限として流用しない。"""
            value = probe.text(key).strip()
            pattern = r"(?:max|\d{1,20}) \d{1,20}" if key == "current_cpu" else r"(?:max|\d{1,20})"
            if not re.fullmatch(pattern, value):
                raise ValueError()
            return fact("OBSERVED", current_process_limit=value, sandbox_enforcement="NOT_RUN")
        facts[key] = attempt(limits)

    def runtime():
        """ELFと期待hashを照合する。digest一致でもversionやsystrapの実行は未確認。"""
        observed = probe.binary(runsc_path)
        if set(observed) != {"architecture", "sha256", "executable_bit"} or not re.fullmatch(r"[0-9a-f]{64}", observed["sha256"]) or observed["executable_bit"] is not True:
            raise ValueError()
        if not machine or observed["architecture"] != machine:
            return fact("FAIL", reason="runtime_architecture_mismatch")
        if expected_sha256 is not None and (not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or expected_sha256 != observed["sha256"]):
            return fact("FAIL", reason="runtime_digest_mismatch")
        return fact("OBSERVED", architecture=machine, sha256=observed["sha256"], expected_digest_match=True if expected_sha256 else "UNKNOWN", version="UNKNOWN", executable="NOT_RUN", systrap="NOT_RUN")
    facts["runsc_file"] = attempt(runtime)

    def storage():
        """指定local FSの空き容量だけを報告する。予約容量やquota検証へ格上げしない。"""
        observed = probe.storage(storage_path)
        if set(observed) != {"filesystem", "total_bytes", "available_bytes", "available_inodes", "quota_mount_option"}:
            raise ValueError()
        if observed["filesystem"] not in ("ext4", "xfs", "btrfs") or type(observed["quota_mount_option"]) is not bool:
            raise ValueError()
        for key in ("total_bytes", "available_bytes", "available_inodes"):
            if type(observed[key]) is not int or not 0 <= observed[key] < 2**80:
                raise ValueError()
        if observed["available_bytes"] > observed["total_bytes"]:
            raise ValueError()
        status = "FAIL" if observed["available_bytes"] == 0 or observed["available_inodes"] == 0 else "OBSERVED"
        return fact(status, **observed, quota_enforcement="NOT_RUN", reservation_capacity="UNKNOWN")
    facts["storage"] = attempt(storage)
    return result


class SafeParser(argparse.ArgumentParser):
    """未知引数の値をargparseのエラーへ表示させないパーサー。"""

    def error(self, message):
        """入力値が秘密でも固定エラーにする。"""
        raise ValueError()


def main(argv=None):
    """既定では調査もしない。明示opt-in時も読取りだけで、終了値2＝実行許可なし。"""
    parser = SafeParser(description="専用Linuxの読取り専用調査。実行許可は発行しません。")
    parser.add_argument("--inspect-dedicated-host", action="store_true")
    parser.add_argument("--runsc-path", default="/usr/local/bin/runsc")
    parser.add_argument("--storage-path")
    parser.add_argument("--expected-runsc-sha256")
    result = report()
    try:
        args = parser.parse_args(argv)
        if args.inspect_dedicated_host and args.storage_path:
            result = collect(LocalProbe(), args.runsc_path, args.storage_path, args.expected_runsc_sha256)
    except Exception:
        result = report()
        result["collection"] = "ERROR"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2  # 調査完了しても実行条件は未検証。CIでlaunch成功に誤変換させない。


if __name__ == "__main__":
    raise SystemExit(main())
