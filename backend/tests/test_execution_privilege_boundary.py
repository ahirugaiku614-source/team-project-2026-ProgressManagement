"""権限分離計画の静的検査。実Docker・実認証・既存データを使用しない。"""

from dataclasses import replace
import unittest
from unittest.mock import patch

from execution_worker.privilege_boundary import BoundaryPlan, review_plan


class PrivilegeBoundaryTests(unittest.TestCase):
    """設計チェックの正常系・拒否系と、副作用・実行許可がないことを確認する。"""

    def test_recommended_plan_is_design_only(self):
        """推奨設定でも実行許可や実機検証済みという結果を返さない。"""
        report = review_plan(BoundaryPlan())
        self.assertEqual(report.status, "DESIGN_ONLY")
        self.assertEqual(report.findings, ())
        self.assertFalse(report.execution_enabled)
        self.assertIn("ROOTLESS_CHAIN", report.not_run)

    def test_comparison_candidates_do_not_enable_execution(self):
        """rootful比較候補は記述できても、既存Driverへ接続する許可にはならない。"""
        for strategy in ("rootful_broker", "daemon_authz"):
            with self.subTest(strategy=strategy):
                report = review_plan(BoundaryPlan(strategy=strategy))
                self.assertEqual(report.status, "DESIGN_ONLY")
                self.assertFalse(report.execution_enabled)

    def test_dangerous_privileges_are_rejected(self):
        """root・socket mount・無制限転送・sudo・host実行・fallbackを全て拒否する。"""
        for name in ("api_root", "socket_mount", "generic_api_forwarding", "broad_sudo",
                     "host_code_execution", "runtime_fallback", "execution_enabled"):
            with self.subTest(field=name):
                report = review_plan(replace(BoundaryPlan(), **{name: True}))
                self.assertEqual(report.status, "REJECTED")
                self.assertIn(name.upper(), report.findings)
                self.assertFalse(report.execution_enabled)

    def test_missing_boundary_or_owner_checks_are_rejected(self):
        """共有daemonや所属・所有者照合を省略する計画を受け入れない。"""
        for name in ("dedicated_host", "dedicated_daemon", "broker_required",
                     "peer_bound_identity", "ownership_ledger", "unknown_holds_slots"):
            with self.subTest(field=name):
                report = review_plan(replace(BoundaryPlan(), **{name: False}))
                self.assertEqual(report.status, "REJECTED")
                self.assertIn(name.upper(), report.findings)

    def test_unknown_strategy_runtime_and_transport_are_rejected(self):
        """不明方式・runc・非認証通信を既定値へフォールバックさせない。"""
        for changes in ({"strategy": "unknown"}, {"runtime": "runc"}, {"transport": "plain_http"}):
            with self.subTest(changes=changes):
                self.assertEqual(review_plan(replace(BoundaryPlan(), **changes)).status, "REJECTED")

    def test_arbitrary_engine_operations_and_capacity_changes_are_rejected(self):
        """操作追加・上限変更を認めず、固定のRUN_PYTHON・4枠を維持する。"""
        for changes in ({"operations": ("RUN_PYTHON", "EXEC")}, {"operations": ()},
                        {"max_concurrent": 5}, {"max_concurrent": 0}):
            with self.subTest(changes=changes):
                self.assertEqual(review_plan(replace(BoundaryPlan(), **changes)).status, "REJECTED")

    def test_type_confusion_and_extra_input_are_rejected(self):
        """bool/int混同、辞書、余分な操作値などを計画として採用しない。"""
        for value in (None, {}, {"secret": "SYNTHETIC_SECRET"},
                      BoundaryPlan(api_root=0), BoundaryPlan(max_concurrent=True),
                      BoundaryPlan(operations=["RUN_PYTHON"])):
            with self.subTest(kind=type(value).__name__):
                report = review_plan(value)
                self.assertEqual(report.status, "REJECTED")
                self.assertNotIn("SYNTHETIC_SECRET", repr(report))

    def test_review_has_no_file_network_or_process_side_effects(self):
        """ファイル読取・外部通信・子process起動が禁止されても静的検査が完了する。"""
        with patch("builtins.open", side_effect=AssertionError("ファイルアクセス禁止")), \
             patch("socket.socket", side_effect=AssertionError("通信禁止")), \
             patch("subprocess.Popen", side_effect=AssertionError("プロセス起動禁止")):
            self.assertEqual(review_plan(BoundaryPlan()).status, "DESIGN_ONLY")

    def test_environment_does_not_enable_execution(self):
        """環境変数に有効化値があっても参照せず、実行を許可しない。"""
        with patch.dict("os.environ", {"DOCKER_HOST": "tcp://invalid:2375", "EXECUTION_ENABLED": "true"}):
            self.assertFalse(review_plan(BoundaryPlan()).execution_enabled)
