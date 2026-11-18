"""学校試験の計算・実行不可・保護ハッシュを、一時JSONとモックだけで確認する。"""

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import school_pilot_check as checker


class SchoolPilotTests(unittest.TestCase):
    """試算の境界値と誤った配備許可を防ぐ。実負荷や実VMを発生させない試験。"""

    def setUp(self):
        """元の設計JSONを読み、新しい私有一時領域だけを変更する。"""
        self.temp = tempfile.TemporaryDirectory(prefix="school-plan-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = json.loads((checker.ROOT / "docs/school-pilot/plan.example.json").read_text(encoding="utf-8"))
        self.path = self.root / "plan.json"

    def evaluate(self):
        """合成計画を一時JSONへ出力して静的検査する。実環境への接続はない。"""
        self.path.write_text(json.dumps(self.plan), encoding="utf-8")
        return checker.evaluate(self.path)

    def test_example_fits_but_never_enables_runtime_or_auth_changes(self):
        """2TiB+外部4TiBの計算成立を実行許可へ変換しない。"""
        report = self.evaluate()
        self.assertEqual(report["status"], "design_consistent")
        self.assertTrue(report["runtime_source_unchanged"])
        for key in ("editor_launch_enabled", "deployment_allowed", "authentication_change_allowed", "performance_verified"):
            self.assertIs(report[key], False)

    def test_forty_users_storage_and_full_backups(self):
        """個人データ800GiB、OS等込み1584GiB、外部2世代3040GiBを再計算する。"""
        report = self.evaluate()["storage"]
        self.assertEqual(report["personal_data_gib"], 800)
        self.assertEqual(report["primary_required_gib"], 1584)
        self.assertEqual(report["external_backup_required_gib"], 3040)
        self.assertEqual(report["local_with_backup_required_gib"], 4624)

    def test_one_tib_is_insufficient_with_and_without_local_backups(self):
        """外部バックアップにしても、全40環境を1TiBに収める計画を合格にしない。"""
        report = self.evaluate()["storage"]
        self.assertFalse(report["one_tib_with_external_backup_fits"])
        self.assertFalse(report["one_tib_with_local_backup_fits"])
        self.plan["primary_gib"] = 1024
        self.assertEqual(self.evaluate()["status"], "capacity_shortfall")

    def test_user_project_pairs_double_storage(self):
        """ユーザー人数だけで容量を数えず、80環境の不足を報告する。"""
        report = self.evaluate()["two_projects_each"]
        self.assertEqual(report["retained_pairs"], 80)
        self.assertEqual(report["primary_required_gib"], 3024)
        self.assertEqual(report["external_backup_required_gib"], 5920)
        self.assertFalse(report["primary_fits"])
        self.assertFalse(report["external_backup_fits"])

    def test_initial_limit_is_four_even_with_two_large_hosts(self):
        """複数ホスト比較に余裕があっても初期全体枠を増やさない。"""
        report = self.evaluate()["hosts"]
        self.assertEqual([h["per_host_budget"] for h in report], [4, 6, 6])
        self.assertEqual([h["aggregate_budget"] for h in report], [4, 6, 12])
        self.assertEqual([h["initial_effective_budget"] for h in report], [4, 4, 4])

    def test_memory_alone_does_not_guarantee_vm_capacity(self):
        """64GiBでもCPU不足なら4台に届かず、計画不成立となる。"""
        self.plan["hosts"][0].update(cpu=8, memory_gib=64)
        self.assertEqual(self.evaluate()["status"], "capacity_shortfall")
        self.assertEqual(checker.host_capacity(2, 4), 0)

    def test_ten_twenty_expansion_is_planning_only(self):
        """将来必要ホスト数とN+1を試算し、現在の上限変更は禁止のままにする。"""
        future = self.evaluate()["future"]
        self.assertEqual([(f["minimum_hosts"], f["n_plus_one_hosts"]) for f in future], [(2, 3), (4, 5)])
        self.assertTrue(all(f["implemented"] is False and f["limit_change_allowed"] is False for f in future))

    def test_backup_capacity_and_twenty_percent_reserve(self):
        """外部バックアップ容量や空き予算を省略して合格にしない。"""
        self.plan["external_backup_gib"] = 2048
        self.assertEqual(self.evaluate()["status"], "capacity_shortfall")
        self.assertEqual(self.evaluate()["storage"]["primary_budget_gib"], 1638)

    def test_exact_storage_boundary(self):
        """20%空き予算の丸めにより不足を見逃さない。"""
        self.plan["primary_gib"] = 1980
        self.assertEqual(self.evaluate()["status"], "design_consistent")
        self.plan["primary_gib"] = 1979
        self.assertEqual(self.evaluate()["status"], "capacity_shortfall")

    def test_numeric_types_ranges_and_flags_are_strict(self):
        """bool=1、負数、小数、巨大値、起動/認証変更の申告を拒否する。"""
        original = copy.deepcopy(self.plan)
        cases = (("registered_users", 41), ("retained_pairs", 39), ("data_gib", 10), ("root_delta_gib", True),
                 ("primary_gib", -1), ("primary_gib", 1.5), ("primary_gib", 10**12),
                 ("backup_generations", 1), ("free_reserve_percent", 0), ("global_running_limit", 10),
                 ("launch_enabled", True), ("authentication_changes", True), ("mode", "LIVE"))
        for key, value in cases:
            self.plan = copy.deepcopy(original)
            self.plan[key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(self.evaluate()["status"], "invalid_design")

    def test_unknown_credentials_or_host_addresses_are_not_input_fields(self):
        """実接続情報を含む未定義フィールドを受け付けず値を反射しない。"""
        self.plan["endpoint"] = "SECRET-PATH https://private.invalid"
        report = self.evaluate()
        self.assertEqual(report["status"], "invalid_design")
        self.assertNotIn("SECRET", json.dumps(report))

    def test_duplicate_json_and_nan_are_refused(self):
        """重複キー・非標準数値をJSONの比較回避に使わせない。"""
        for value in ('{"version":1,"version":1}', '{"version":NaN}'):
            self.path.write_text(value, encoding="utf-8")
            self.assertEqual(checker.evaluate(self.path)["status"], "invalid_design")

    def test_no_network_commands_or_writes_to_input(self):
        """通信・外部コマンドを禁止した状態でも検査でき、入力ファイルは書き換えない。"""
        self.evaluate()
        before = self.path.read_bytes()
        with patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("subprocess.run", side_effect=AssertionError("CLI")):
            self.assertEqual(checker.evaluate(self.path)["status"], "design_consistent")
        self.assertEqual(before, self.path.read_bytes())

    def test_protected_source_mismatch_never_allows_execution(self):
        """一時コピーで保護ソース不一致を注入し、拒否できることを確認する。"""
        self.evaluate()
        app = self.root / "backend/app"
        app.mkdir(parents=True)
        (app / "editor_contracts.py").write_text("# synthetic changed source", encoding="utf-8")
        report = checker.evaluate(self.path, root=self.root)
        self.assertEqual(report["status"], "invalid_design")
        self.assertIs(report["runtime_source_unchanged"], False)
        self.assertIs(report["editor_launch_enabled"], False)

    def test_cli_rejects_deploy_option_without_echoing_secret(self):
        """配備・実行のCLI入口がなく、不明引数の値も表示しない。"""
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(checker.main(["--deploy", "SECRET"]), 2)
        self.assertNotIn("SECRET", output.getvalue())

    def test_cli_reports_calculations_only(self):
        """通常CLIも試算出力のみで、性能・配備許可を生成しない。"""
        self.evaluate()
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(checker.main(["--plan", str(self.path)]), 0)
        report = json.loads(output.getvalue())
        self.assertIs(report["performance_verified"], False)


if __name__ == "__main__":
    unittest.main()
