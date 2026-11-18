"""第11段階の人数・CPU/RAM・保存容量の試算を静的検証する。実機操作は一切しない。

既存の安全なJSON読取り・保護ハッシュ検査を再利用し、既存設計JSONも変更しない。
計算が成立しても、性能合格・配備許可・エディタ起動許可へ変換しない。
"""

import json
from pathlib import Path

from tools.editor_foundation_check import (
    InvalidDesign, ROOT, SafeParser, check_runtime_source, exact, load_json, shape,
)

FIELDS = (
    "version", "mode", "launch_enabled", "authentication_changes", "global_running_limit",
    "registered_users", "retained_pairs", "data_gib", "root_delta_gib", "shared_base_gib",
    "system_gib", "recovery_gib", "backup_generations", "backup_metadata_gib", "app_backup_gib",
    "free_reserve_percent", "primary_gib", "external_backup_gib", "hosts",
)


def integer(value, minimum=1, maximum=100000):
    """真偽値・小数・負数・巨大値を計算入力に使わせず、固定コードで拒否する。"""
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidDesign("invalid_pilot_number")


def validate(plan):
    """学校試験用の計画形式と全体4枠を確認する。実行設定を取り込む関数ではない。"""
    shape(plan, FIELDS)
    for key, expected in (("version", 1), ("mode", "DESIGN_ONLY"), ("launch_enabled", False),
                          ("authentication_changes", False), ("global_running_limit", 4)):
        exact(plan[key], expected, "design_only_required")
    for key in FIELDS[5:-1]:
        integer(plan[key])
    integer(plan["registered_users"], 30, 40)
    integer(plan["retained_pairs"], plan["registered_users"], 4000)
    exact(plan["data_gib"], 20, "data_quota_mismatch")
    integer(plan["free_reserve_percent"], 20, 50)
    integer(plan["backup_generations"], 2, 10)
    if type(plan["hosts"]) is not list or len(plan["hosts"]) != 3:
        raise InvalidDesign("invalid_host_comparison")
    for host, label in zip(plan["hosts"], ("A", "B", "C")):
        shape(host, ("id", "count", "cpu", "memory_gib"))
        exact(host["id"], label, "invalid_host_comparison")
        for key in ("count", "cpu", "memory_gib"):
            integer(host[key], 1, 1024)


def storage(plan, pairs):
    """停止中も消えないUser×Project全組数で、圧縮・thin節約を見込まず最大予約量を計算する。"""
    integer(pairs, 1, 4000)
    volumes = pairs * (plan["data_gib"] + plan["root_delta_gib"])
    primary = plan["system_gib"] + plan["recovery_gib"] + plan["shared_base_gib"] + volumes
    backup = volumes * plan["backup_generations"] + plan["backup_metadata_gib"] + plan["app_backup_gib"]
    usable_primary = plan["primary_gib"] * (100 - plan["free_reserve_percent"]) // 100
    usable_backup = plan["external_backup_gib"] * (100 - plan["free_reserve_percent"]) // 100
    return {
        "retained_pairs": pairs, "personal_data_gib": pairs * plan["data_gib"],
        "primary_required_gib": primary, "external_backup_required_gib": backup,
        "local_with_backup_required_gib": primary + backup,
        "primary_budget_gib": usable_primary, "external_backup_budget_gib": usable_backup,
        "primary_fits": primary <= usable_primary, "external_backup_fits": backup <= usable_backup,
        "one_tib_with_external_backup_fits": primary <= 1024 * (100 - plan["free_reserve_percent"]) // 100,
        "one_tib_with_local_backup_fits": primary + backup <= 1024 * (100 - plan["free_reserve_percent"]) // 100,
    }


def host_capacity(cpu, memory_gib):
    """ホスト予約4 CPU/8GiB、VM予算2 CPU/4GiBで過剰割当てしない理論上限を返す。

    vCPUは物理コアの性能保証ではない。数値上限はベンチマーク結果でも起動許可でもない。
    """
    integer(cpu, 1, 1024)
    integer(memory_gib, 1, 100000)
    return max(0, min((cpu - 4) // 2, (memory_gib - 8) // 4))


def expansion(target, capacity_per_host):
    """将来10/20台の通常・N+1 CPU/RAM予算を計算する。分散枠管理は実装しない。"""
    integer(target, 1, 1000)
    integer(capacity_per_host, 1, 1000)
    normal = (target + capacity_per_host - 1) // capacity_per_host
    return {"target": target, "minimum_hosts": normal, "n_plus_one_hosts": normal + 1,
            "implemented": False, "limit_change_allowed": False}


def evaluate(path, root=ROOT):
    """保護ソースと計画を検査し、常に実行・認証変更不可の試算レポートを返す。"""
    result = {"status": "invalid_design", "editor_launch_enabled": False, "deployment_allowed": False,
              "authentication_change_allowed": False, "performance_verified": False,
              "runtime_source_unchanged": False, "errors": []}
    try:
        check_runtime_source(root)
        result["runtime_source_unchanged"] = True
        plan = load_json(path)
        validate(plan)
        retained = storage(plan, plan["retained_pairs"])
        result["storage"] = retained
        result["two_projects_each"] = storage(plan, plan["registered_users"] * 2)
        result["hosts"] = [{"id": h["id"], "per_host_budget": host_capacity(h["cpu"], h["memory_gib"]),
                            "aggregate_budget": host_capacity(h["cpu"], h["memory_gib"]) * h["count"],
                            "initial_global_limit": 4,
                            "initial_effective_budget": min(4, host_capacity(h["cpu"], h["memory_gib"]) * h["count"])}
                           for h in plan["hosts"]]
        result["future"] = [expansion(target, host_capacity(16, 64)) for target in (10, 20)]
        # 容量の計算不足は計画不成立として示す。ただし1TiB比較の不足は意図した比較結果。
        result["status"] = "design_consistent" if retained["primary_fits"] and retained["external_backup_fits"] and all(
            host["initial_effective_budget"] == 4 for host in result["hosts"]) else "capacity_shortfall"
    except InvalidDesign as error:
        result["errors"] = [str(error)]
    except (OSError, TypeError, ValueError, KeyError, RecursionError):
        result["errors"] = ["input_unavailable"]
    return result


def main(argv=None):
    """計画ファイルの読取りと標準出力だけのCLI。配備・課金・認証変更の引数は存在しない。"""
    parser = SafeParser(description="学校試験の静的容量試算。実行・配備・性能保証は行いません。")
    parser.add_argument("--plan", type=Path, default=ROOT / "docs/school-pilot/plan.example.json")
    try:
        args = parser.parse_args(argv)
        report = evaluate(args.plan)
    except InvalidDesign:
        report = {"status": "invalid_design", "editor_launch_enabled": False, "deployment_allowed": False,
                  "authentication_change_allowed": False, "performance_verified": False, "errors": ["invalid_arguments"]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "design_consistent" else 2


if __name__ == "__main__":
    raise SystemExit(main())
