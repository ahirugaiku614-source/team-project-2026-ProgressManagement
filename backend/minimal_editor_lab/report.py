"""1→2→4人の予約だけを一時台帳で試すCLI。プログラム実行や性能測定は行わない。"""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from app.editor_contracts import EditorBinding
from minimal_editor_lab.contracts import Budget, LabDenied, PROFILES, StorageEvidence
from minimal_editor_lab.ledger import Ledger
from minimal_editor_lab.model import ResourceModel
from tools.editor_foundation_check import ROOT, SafeParser, check_runtime_source


class SimulationAuthorizer:
    """合成の所有者一致だけを許可するCLI専用モック。Firebase本人確認の代替ではない。"""

    def allowed(self, actor, binding, kind):
        """固定Team/Projectの試験用IDだけを許可し、外部サービスを照会しない。"""
        return type(actor) is int and binding.team_id == 1 and binding.project_id == 1 and actor == binding.user_id


def simulate(budget, profile, target):
    """新しい一時台帳へ最大4人の予約を試みる。部分成功も件数で返し、実機成功としない。"""
    if type(target) is not int or target not in (1, 2, 4):
        raise LabDenied()
    with tempfile.TemporaryDirectory(prefix="minimal-editor-simulation-") as temporary:
        ledger = Ledger.create_new(Path(temporary) / "ledger.sqlite", budget)
        model = ResourceModel(ledger, SimulationAuthorizer(), lambda: 1000000)
        accepted = 0
        for user in range(1, target + 1):
            binding = EditorBinding(team_id=1, project_id=1, user_id=user)
            try:
                model.prepare_start(user, binding, profile, uuid4(), StorageEvidence(budget.disk_gib, 1000000, True))
                accepted += 1
            except LabDenied:
                pass
        return {"profile": asdict(profile), "requested": target, "reserved": accepted,
                "rejected": target - accepted, "real_vm": "NOT_RUN", "real_container": "NOT_RUN",
                "code_server": "NOT_RUN", "toolchains": {tool: "NOT_RUN" for tool in ("python", "gcc", "node", "git")},
                "performance_verified": False}


def evaluate():
    """保護ハッシュと固定予算のシミュレーションだけを出力し、許可値は必ずfalseにする。"""
    check_runtime_source(ROOT)
    budgets = {
        "dedicated_host_after_reserves": Budget(8, 24576, 1494),
        # 200GBをGiBへ換算し20%を空け、host root50GiB+scratch16GiBを予約した例。
        # CPU1/RAM2GiBの制御余裕も差し引く。実際のOCI適合性・単位は未検証。
        "illustrative_small_host_after_reserves": Budget(1, 10240, 83),
    }
    return {"status": "SIMULATION_ONLY", "launch_enabled": False, "deployment_allowed": False,
            "cloud_verified": False, "runtime_source_unchanged": True,
            "scenarios": {name: [simulate(budget, profile, count) for profile in PROFILES for count in (1, 2, 4)]
                          for name, budget in budgets.items()}}


def main(argv=None):
    """引数による実行/配備切替を持たないCLI。失敗時も秘密値や内部パスを表示しない。"""
    parser = SafeParser(description="一時台帳の予約シミュレーションのみ。実機はNOT_RUN。")
    try:
        parser.parse_args(argv)
        result = evaluate()
        code = 0
    except Exception:
        result = {"status": "DENIED", "launch_enabled": False, "deployment_allowed": False, "cloud_verified": False,
                  "error": "simulation_unavailable"}
        code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
