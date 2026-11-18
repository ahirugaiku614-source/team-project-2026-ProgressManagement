"""最小構成を既存データから独立した一時台帳/永続モック/仮想時計で検証する。"""

from dataclasses import replace
import multiprocessing
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.editor_contracts import EditorBinding, EditorRuntimeUnavailable, require_editor_runtime
from editor_gateway.gateway import Gateway, challenge_for
from editor_gateway.store import TestSessionStore
from editor_gateway_fakes import Authority, editor_context, exchange_context, issue_context
from minimal_editor_lab.contracts import Budget, LabDenied, PROFILES, Profile, StorageEvidence, require_real_runtime
from minimal_editor_lab.ledger import Ledger
from minimal_editor_lab.model import ResourceModel
from minimal_editor_fakes import Clock, FrozenBoundary, Members, PersistentFakeDriver, concurrent_reserve, evidence_for
from minimal_editor_lab import report


class MinimalEditorTests(unittest.TestCase):
    """資源・所有者・停止確認の境界を検証する。実エディタ性能の試験ではない。"""

    def setUp(self):
        """新規一時ディレクトリだけに試験台帳と合成disk行を作る。"""
        self.temp = tempfile.TemporaryDirectory(prefix="minimal-editor-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock(1000000)
        self.bindings = [EditorBinding(team_id=1, project_id=1, user_id=i) for i in range(1, 9)]
        self.binding = self.bindings[0]
        self.members = Members(self.bindings)
        self.ledger = Ledger.create_new(self.root / "reservations.sqlite", Budget(8, 24576, 1494))
        self.model = ResourceModel(self.ledger, self.members, self.clock)
        self.driver = PersistentFakeDriver.create_new(self.root / "fake.sqlite", self.clock)
        self.boundary = FrozenBoundary(self.driver, self.clock)

    def prepare(self, binding=None, profile=None, operation_id=None, storage=None):
        """現在時刻の合成容量証跡を添えて新規予約する。実作成はしない。"""
        binding = binding or self.binding
        return self.model.prepare_start(binding.user_id, binding, profile or PROFILES[3], operation_id or uuid4(),
                                        storage if storage is not None else StorageEvidence(1494, self.clock(), True))

    def start(self, binding=None, profile=None):
        """予約Fenceを既存永続モックで完了させ、契約互換性を検査する。"""
        fence, _ = self.prepare(binding, profile)
        return self.model.finish(fence.command.binding.user_id, fence, self.driver.execute(fence))

    def stop(self, binding=None):
        """静止証跡→停止意図→最終境界確認→保存をモックだけで順に実施する。"""
        binding = binding or self.binding
        row = self.model.state(binding.user_id, binding)
        evidence = evidence_for(binding, row["generation"], self.clock)
        plan, _ = self.model.prepare_idle_stop(binding.user_id, binding, uuid4(), evidence)
        ack = self.boundary.stop(plan)
        return self.model.finish(binding.user_id, plan.fence, ack.observation, ack)

    def test_one_two_four_users_for_each_profile_and_fifth_refused(self):
        """全4profileを十分な予算で1→2→4予約し、5組目は全体枠で拒否する。"""
        for index, profile in enumerate(PROFILES):
            ledger = Ledger.create_new(self.root / f"profile-{index}.sqlite", Budget(8, 16384, 200))
            model = ResourceModel(ledger, self.members, self.clock)
            for count, binding in enumerate(self.bindings[:4], 1):
                model.prepare_start(binding.user_id, binding, profile, uuid4(), StorageEvidence(200, self.clock(), True))
                self.assertEqual(len(ledger.snapshot()["environments"]), count)
            with self.assertRaises(LabDenied):
                model.prepare_start(5, self.bindings[4], profile, uuid4(), StorageEvidence(200, self.clock(), True))

    def test_cpu_and_memory_limits_are_independent(self):
        """CPU/RAMのどちらか片方が足りなくても拒否し、途中行を残さない。"""
        for index, budget in enumerate((Budget(1, 8192, 200), Budget(8, 1024, 200))):
            ledger = Ledger.create_new(self.root / f"small-{index}.sqlite", budget)
            model = ResourceModel(ledger, self.members, self.clock)
            with self.assertRaises(LabDenied):
                model.prepare_start(1, self.binding, PROFILES[3], uuid4(), StorageEvidence(200, self.clock(), True))
            self.assertEqual(ledger.snapshot()["environments"], [])

    def test_same_operation_replayed_without_redispatch_new_operation_rejected(self):
        """同じIDは履歴だけを返し、同じ本人の別ID予約を増やさない。"""
        operation = uuid4()
        first, _ = self.prepare(operation_id=operation)
        replay, result = self.prepare(operation_id=operation)
        self.assertIsNone(replay)
        self.assertEqual(result["operation"]["status"], "ACTIVE")
        with self.assertRaises(LabDenied):
            self.prepare()
        self.model.finish(1, first, self.driver.execute(first))
        with self.assertRaises(LabDenied):
            self.prepare()
        self.assertEqual(len(self.driver.rows()), 1)

    def test_operation_id_cannot_be_rebound_to_other_owner(self):
        """同じ操作IDを別Userへ付け替えて予約しない。"""
        operation = uuid4()
        self.prepare(operation_id=operation)
        with self.assertRaises(LabDenied):
            self.prepare(binding=self.bindings[1], operation_id=operation)

    def test_user_and_project_have_independent_vm_and_disk(self):
        """同じUserの別Projectも別Userも、VM/diskの安定IDが異なる。"""
        other = EditorBinding(team_id=1, project_id=2, user_id=1)
        self.members.bindings.add((1, 2, 1))
        for binding in (self.binding, self.bindings[1], other):
            self.start(binding)
        rows = self.driver.rows()
        self.assertEqual(len({r["vm_id"] for r in rows}), 3)
        self.assertEqual(len({r["disk_id"] for r in rows}), 3)

    def test_actor_membership_team_and_default_authorizer_fail_closed(self):
        """他人・所属外・Team差替え・未接続検証器を拒否する。"""
        with self.assertRaises(LabDenied):
            self.model.state(2, self.binding)
        self.start()
        other = EditorBinding(team_id=2, project_id=1, user_id=1)
        self.members.bindings.add((2, 1, 1))
        with self.assertRaises(LabDenied):
            self.model.state(1, other)
        self.members.bindings.clear()
        with self.assertRaises(LabDenied):
            self.model.state(1, self.binding)
        with self.assertRaises(LabDenied):
            ResourceModel(self.ledger, clock_ms=self.clock).state(1, self.binding)

    def test_stop_restart_keeps_disk_data_and_increases_generation(self):
        """正常停止後もdisk予約/内容を保持し、新世代でのみ再開する。"""
        first = self.start()
        stopped = self.stop()
        self.assertEqual(stopped["environment"]["state"], "STOPPED")
        second = self.start()
        self.assertGreater(second["environment"]["generation"], first["environment"]["generation"])
        self.assertEqual(second["environment"]["disk_id"], first["environment"]["disk_id"])
        self.assertEqual(self.driver.rows()[0]["content"], "synthetic-edit-data")
        self.assertEqual(len(self.driver.rows()), 1)

    def test_stopped_disk_still_counts_and_restart_does_not_double_reserve(self):
        """停止で保存予約は戻さない。既存diskの再起動は追加diskを要求しない。"""
        ledger = Ledger.create_new(self.root / "one-disk.sqlite", Budget(8, 16384, 36))
        self.model = ResourceModel(ledger, self.members, self.clock)
        self.start()
        self.stop()
        with self.assertRaises(LabDenied):
            self.prepare(self.bindings[1])
        self.start()
        self.assertEqual(len(ledger.snapshot()["environments"]), 1)

    def test_storage_health_staleness_capacity_and_missing_proof_refused(self):
        """保存状態不明・容量不足・古い/未来の観測を新規起動の根拠にしない。"""
        for evidence in (None, StorageEvidence(35, self.clock(), True), StorageEvidence(200, self.clock(), False),
                         StorageEvidence(200, self.clock() - 5001, True), StorageEvidence(200, self.clock() + 1, True)):
            with self.assertRaises(LabDenied):
                self.model.prepare_start(1, self.binding, PROFILES[0], uuid4(), evidence)
        self.assertEqual(self.ledger.snapshot()["environments"], [])

    def test_jobs_clients_dirty_saving_and_unknown_prohibit_idle_stop(self):
        """画面を閉じてもjob/dirtyがあれば止めず、欠測を安全と推定しない。"""
        self.start()
        row = self.model.state(1, self.binding)
        proof = evidence_for(self.binding, row["generation"], self.clock)
        for changes in ({"jobs": 1}, {"jobs": None}, {"jobs": False}, {"clients": 1}, {"clients": None},
                        {"dirty": True}, {"dirty": None}, {"saving": True}, {"saved_ack": False}, {"saved_ack": None}):
            with self.subTest(changes=changes), self.assertRaises(LabDenied):
                self.model.prepare_idle_stop(1, self.binding, uuid4(), replace(proof, **changes))
        self.assertEqual(self.model.state(1, self.binding)["state"], "RUNNING")

    def test_idle_period_freshness_owner_and_generation_required(self):
        """10分未満/古い証跡/別所有者/旧世代の停止意図を作らない。"""
        self.start()
        proof = evidence_for(self.binding, 1, self.clock)
        for changes in ({"last_activity_ms": self.clock() - 599999}, {"sampled_ms": self.clock() - 5001},
                        {"generation": 2}, {"binding": self.bindings[1]}, {"quiescence_id": "invalid"}):
            with self.assertRaises(LabDenied):
                self.model.prepare_idle_stop(1, self.binding, uuid4(), replace(proof, **changes))

    def test_job_or_edit_after_decision_rejected_at_stop_boundary(self):
        """判断直後のjob/編集発生で境界のrevisionが変われば停止せず隔離する。"""
        self.start()
        proof = evidence_for(self.binding, 1, self.clock)
        plan, _ = self.model.prepare_idle_stop(1, self.binding, uuid4(), proof)
        self.boundary.jobs, self.boundary.revision = 1, 2
        with self.assertRaises(LabDenied):
            self.boundary.stop(plan)
        self.model.uncertain(1, plan.fence)
        self.assertEqual(self.driver.rows()[0]["vm_state"], "RUNNING")
        self.assertEqual(self.model.state(1, self.binding)["state"], "QUARANTINED")

    def test_dirty_edit_after_decision_never_reaches_stop_effect(self):
        """判断後の未保存編集は、revisionが変わらなくても境界で拒否する。"""
        self.start()
        plan, _ = self.model.prepare_idle_stop(1, self.binding, uuid4(), evidence_for(self.binding, 1, self.clock))
        self.boundary.dirty = True
        with self.assertRaises(LabDenied):
            self.boundary.stop(plan)
        self.assertEqual(self.driver.rows()[0]["vm_state"], "RUNNING")

    def test_expired_successful_stop_reply_keeps_slot_quarantined(self):
        """停止成功後でもlease後の返答は不明扱いにし、勝手に枠を戻さない。"""
        self.start()
        plan, _ = self.model.prepare_idle_stop(1, self.binding, uuid4(), evidence_for(self.binding, 1, self.clock))
        ack = self.boundary.stop(plan)
        self.clock.now += 30000
        result = self.model.finish(1, plan.fence, ack.observation, ack)
        self.assertEqual(result["environment"]["state"], "QUARANTINED")

    def test_missing_or_changed_stop_ack_never_releases_slot(self):
        """停止応答だけでは足りず、同じ保存/凍結確認が必要。"""
        self.start()
        plan, _ = self.model.prepare_idle_stop(1, self.binding, uuid4(), evidence_for(self.binding, 1, self.clock))
        ack = self.boundary.stop(plan)
        result = self.model.finish(1, plan.fence, ack.observation, replace(ack, revision=2))
        self.assertEqual(result["environment"]["state"], "QUARANTINED")
        self.assertEqual(self.driver.rows()[0]["content"], "synthetic-edit-data")

    def test_nonce_cannot_be_reused_after_restart(self):
        """静止nonceは別停止操作に使い回せず、新しい凍結確認が必要。"""
        self.start()
        proof = evidence_for(self.binding, 1, self.clock)
        plan, _ = self.model.prepare_idle_stop(1, self.binding, uuid4(), proof)
        ack = self.boundary.stop(plan)
        self.model.finish(1, plan.fence, ack.observation, ack)
        self.start()
        with self.assertRaises(LabDenied):
            self.model.prepare_idle_stop(1, self.binding, uuid4(), replace(proof, generation=3))
        self.assertEqual(self.model.state(1, self.binding)["state"], "RUNNING")

    def test_timeout_and_process_restart_keep_resources(self):
        """期限切れ・台帳再開でも予約は残り、新しいVMを重ねない。"""
        self.prepare()
        self.clock.now += 30000
        self.model = ResourceModel(Ledger(self.ledger.path), self.members, self.clock)
        self.assertEqual(self.model.state(1, self.binding)["state"], "QUARANTINED")
        with self.assertRaises(LabDenied):
            self.prepare()
        for binding in self.bindings[1:4]:
            self.prepare(binding)
        with self.assertRaises(LabDenied):
            self.prepare(self.bindings[4])

    def test_lost_stop_response_holds_capacity_and_data(self):
        """モック停止後に応答が消失しても、枠を返却しない。"""
        self.start()
        plan, _ = self.model.prepare_idle_stop(1, self.binding, uuid4(), evidence_for(self.binding, 1, self.clock))
        self.boundary.stop(plan)
        self.model.uncertain(1, plan.fence)
        for binding in self.bindings[1:4]:
            self.prepare(binding)
        with self.assertRaises(LabDenied):
            self.prepare(self.bindings[4])
        self.assertEqual(self.driver.rows()[0]["content"], "synthetic-edit-data")

    def test_stale_fence_or_extended_lease_cannot_complete(self):
        """古い世代や偽の期限延長で結果を確定しない。"""
        fence, _ = self.prepare()
        observation = self.driver.execute(fence)
        with self.assertRaises(LabDenied):
            self.model.finish(1, replace(fence, lease_until_ms=fence.lease_until_ms + 1), observation)
        self.model.uncertain(1, fence)
        with self.assertRaises(LabDenied):
            self.model.finish(1, fence, observation)

    def test_wrong_observation_owner_is_rejected_without_data_change(self):
        """別Userの完了証跡を受理しない。実行中予約は保持する。"""
        fence, _ = self.prepare()
        observation = self.driver.execute(fence)
        with self.assertRaises(LabDenied):
            self.model.finish(1, fence, replace(observation, disk_owner=self.bindings[1]))
        self.assertEqual(self.model.state(1, self.binding)["state"], "STARTING")

    def test_db_failure_before_and_after_mock_effect_retains_intent(self):
        """副作用前のDB失敗は全予約を戻し、副作用後はACTIVEのまま保全する。"""
        with self.ledger.connection() as db:
            db.execute("CREATE TRIGGER fail_intent BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT,'secret'); END")
        with self.assertRaises(LabDenied):
            self.prepare()
        self.assertEqual(self.ledger.snapshot()["environments"], [])
        with self.ledger.connection() as db:
            db.execute("DROP TRIGGER fail_intent")
        fence, _ = self.prepare()
        observation = self.driver.execute(fence)
        with self.ledger.connection() as db:
            db.execute("CREATE TRIGGER fail_finish BEFORE UPDATE ON operations BEGIN SELECT RAISE(ABORT,'secret'); END")
        with self.assertRaises(LabDenied):
            self.model.finish(1, fence, observation)
        self.assertEqual(self.ledger.snapshot()["operations"][0]["status"], "ACTIVE")
        self.assertEqual(len(self.driver.rows()), 1)

    def test_default_gate_and_network_are_disabled(self):
        """モック検証中も既存/新規ゲートは拒否し、ネットワーク/コマンドを呼ばない。"""
        with patch("socket.create_connection", side_effect=AssertionError("network")), patch("subprocess.run", side_effect=AssertionError("CLI")):
            self.start()
            self.stop()
            with self.assertRaises(LabDenied):
                require_real_runtime()
            with self.assertRaises(EditorRuntimeUnavailable):
                require_editor_runtime()

    def test_gateway_contract_rejects_mock_running_and_old_generation(self):
        """模擬RUNNINGを実readyにせず、承認済みモック接続でも世代変更後は拒否する。"""
        self.start()
        model = self.model
        class Directory:
            """契約試験のときだけsafe_readyを注入する読取りモック。実VMには接続しない。"""
            ready = False
            def lookup(inner, binding):
                route = model.unavailable_route(binding.user_id, binding)
                return replace(route, safe_ready=inner.ready, valid_until_ms=model.clock_ms() + 15000)
        directory, authority = Directory(), Authority()
        authority.identity = replace(authority.identity, token_expires_ms=self.clock() + 1000000)
        gateway = Gateway(TestSessionStore.create_new(self.root / "sessions.sqlite"), authority, directory, self.clock)
        verifier = "x" * 43
        from editor_gateway.contracts import AccessDenied
        with self.assertRaises(AccessDenied):
            gateway.issue("synthetic-id-token", 1, challenge_for(verifier), issue_context())
        directory.ready = True
        code = gateway.issue("synthetic-id-token", 1, challenge_for(verifier), issue_context())
        grant = gateway.exchange(code.code, verifier, exchange_context(code.host))
        gateway.authorize(grant.session, editor_context(grant.host))
        self.stop()
        self.start()
        with self.assertRaises(AccessDenied):
            gateway.authorize(grant.session, editor_context(grant.host))

    def test_input_profiles_and_existing_files_are_protected(self):
        """未定義profile・bool上限・既存ファイル上書きを拒否する。"""
        for args in ((True, 1024), (1, 512), (4, 4096)):
            with self.assertRaises(LabDenied):
                Profile(*args)
        with self.assertRaises(LabDenied):
            Budget(8, 8192, 200, 5)
        path = self.root / "unrelated"
        path.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            Ledger.create_new(path, Budget(8, 8192, 200))
        self.assertEqual(path.read_bytes(), b"keep")

    def test_clock_rollback_denies_new_operations(self):
        """時計逆行時に期限やアイドル判定を延命しない。"""
        self.start()
        self.clock.now -= 1
        with self.assertRaises(LabDenied):
            self.model.state(1, self.binding)

    def test_schema_rejects_null_resources_and_broken_active_state(self):
        """NULL資源・不正状態・参照切れが予約計数から消えてしまうことをDB制約で防ぐ。"""
        self.start()
        for statement in ("UPDATE environments SET cpu=NULL", "UPDATE environments SET state=NULL",
                          "UPDATE environments SET state='STARTING'", "UPDATE operations SET project_id=999"):
            with self.assertRaises(LabDenied):
                with self.ledger.connection() as db:
                    db.execute(statement)
        self.assertEqual(self.model.state(1, self.binding)["state"], "RUNNING")

    def test_simulation_report_never_claims_real_execution(self):
        """十分な予算と小さな予算の全profile/人数試算は、実機成功に変換しない。"""
        with patch("socket.create_connection", side_effect=AssertionError("network")):
            result = report.evaluate()
        self.assertIs(result["launch_enabled"], False)
        self.assertIs(result["deployment_allowed"], False)
        self.assertIs(result["cloud_verified"], False)
        self.assertTrue(result["runtime_source_unchanged"])
        for scenario in result["scenarios"].values():
            self.assertEqual(len(scenario), 12)
            for row in scenario:
                self.assertEqual(row["real_vm"], "NOT_RUN")
                self.assertEqual(row["real_container"], "NOT_RUN")
                self.assertTrue(all(value == "NOT_RUN" for value in row["toolchains"].values()))
                self.assertIs(row["performance_verified"], False)
        self.assertEqual(result["scenarios"]["dedicated_host_after_reserves"][-1]["reserved"], 4)
        self.assertEqual(result["scenarios"]["illustrative_small_host_after_reserves"][-1]["reserved"], 0)

    def test_cli_has_no_deploy_option_and_hides_argument_values(self):
        """実作成の引数を持たず、不明な秘密値も表示しない。"""
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(report.main(["--deploy", "SECRET"]), 2)
        self.assertNotIn("SECRET", output.getvalue())

    def test_cli_reports_only_simulated_reservations(self):
        """CLIも一時台帳の試算だけを返す。"""
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(report.main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "SIMULATION_ONLY")

    def test_multiple_processes_cannot_overreserve_cpu(self):
        """8プロセスが競合しても、2CPUの試験予算には2件だけ予約できる。"""
        ledger = Ledger.create_new(self.root / "shared.sqlite", Budget(2, 16384, 500))
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(8), context.Queue()
        children = [context.Process(target=concurrent_reserve, args=(str(ledger.path), i, str(uuid4()), barrier, queue)) for i in range(1, 9)]
        try:
            for child in children:
                child.start()
            results = [queue.get(timeout=40) for _ in children]
            for child in children:
                child.join(timeout=20)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(results.count("reserved"), 2, results)
            self.assertEqual(len(ledger.snapshot()["environments"]), 2)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=10)
            queue.close()
            queue.join_thread()


if __name__ == "__main__":
    unittest.main()
