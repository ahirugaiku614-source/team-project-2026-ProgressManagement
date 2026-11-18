"""gVisor参照実装の契約試験。実Engine・ネットワーク・実ユーザーデータは使わない。"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import importlib.util
import io
import json
import multiprocessing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.editor_contracts import EditorBinding
from gvisor_lab.check import evaluate, main
from gvisor_lab.lifecycle import Adapter, SaveProof
from gvisor_lab.policy import Budget, Denied, Profile, identities, plan_for, validate_plan
from gvisor_lab.store import Store
from tools.editor_foundation_check import ROOT
from gvisor_fakes import Clock, FakeExecutor, Members, capabilities, competing_prepare


class GvisorLabTests(unittest.TestCase):
    """一時台帳と合成executorで、拒否・予約・停止・復元の境界を試験する。"""

    def setUp(self):
        """試験ごとに独立ファイルを作る。既存の保存領域は参照しない。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "lab.sqlite"
        self.store = Store.create_new(self.path, Budget(8, 16384, 500))
        self.clock = Clock()
        self.binding = EditorBinding(team_id=1, project_id=1, user_id=1)
        self.members = Members([self.binding])
        self.executor = FakeExecutor(self.clock)
        self.adapter = Adapter(self.store, self.members, self.executor, self.clock)
        self.profile = Profile(2, 2048)

    def start(self, binding=None, operation=None):
        """現在の合成所属にある本人の起動を要求する。"""
        b = binding or self.binding
        return self.adapter.request(b.user_id, b, self.profile, operation or uuid4(), capabilities(b, self.clock))

    def proof(self, **changes):
        """保存集約器の代わりに、現在世代の静止証跡を作る。実証明ではない。"""
        row = self.adapter.state(1, self.binding)["environment"]
        return replace(SaveProof(self.binding, row["generation"], self.clock(), uuid4(), 1, 0, False, True), **changes)

    def test_start_is_mock_only_and_duplicate_is_not_dispatched(self):
        op = uuid4()
        result = self.start(operation=op)
        self.assertEqual(result["environment"]["state"], "RUNNING")
        self.assertFalse(result["connectable"])
        self.assertFalse(result["launch_enabled"])
        self.assertEqual(result["real_runsc"], "NOT_RUN")
        self.assertNotIn("vm_id", result["environment"])
        self.assertEqual(self.start(operation=op), result)
        self.assertEqual(len(self.executor.calls), 1)
        with self.assertRaises(Denied):
            self.start()

    def test_stop_restart_preserves_data_and_advances_generation(self):
        self.start()
        key = identities(self.binding)[0]
        self.executor.rows[key]["data"] = "my-synthetic-edit"
        result = self.adapter.request(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock), self.proof())
        self.assertEqual(result["environment"]["state"], "STOPPED")
        with self.store.transaction(self.clock()) as state:
            self.assertEqual(self.adapter.checked(state), (0, 0, 36, 0))
        self.assertEqual(self.start()["environment"]["generation"], 3)
        self.assertEqual(self.executor.rows[key]["data"], "my-synthetic-edit")

    def test_stop_replay_does_not_stop_restarted_environment(self):
        self.start()
        proof, op = self.proof(), uuid4()
        self.adapter.request(1, self.binding, self.profile, op, capabilities(self.binding, self.clock), proof)
        self.start()
        result = self.adapter.request(1, self.binding, self.profile, op, capabilities(self.binding, self.clock), proof)
        self.assertEqual(result["operation"]["ticket"]["generation"], 2)
        self.assertEqual(result["environment"]["generation"], 3)
        self.assertEqual(result["environment"]["state"], "RUNNING")
        self.assertEqual(len(self.executor.calls), 3)

    def test_user_and_project_identities_are_independent(self):
        bindings = [self.binding, EditorBinding(team_id=1, project_id=1, user_id=2), EditorBinding(team_id=1, project_id=2, user_id=1)]
        self.members = Members(bindings)
        self.adapter.authorizer = self.members
        for b in bindings:
            self.start(b)
        self.assertEqual(len(self.executor.rows), 3)
        self.assertEqual(len({identities(b)[1] for b in bindings}), 3)
        with self.assertRaises(Denied):
            self.adapter.state(1, bindings[1])

    def test_revoked_membership_denies_state_and_completion(self):
        ticket, _ = self.adapter.prepare(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock))
        observation = self.executor.execute(ticket, plan_for(self.binding, self.profile, capabilities(self.binding, self.clock).image_digest))
        self.members.bindings.clear()
        with self.assertRaises(Denied):
            self.adapter.state(1, self.binding)
        with self.assertRaises(Denied):
            self.adapter.finish(1, ticket, observation)

    def test_defaults_deny_authorization_and_execution(self):
        with self.assertRaises(Denied):
            Adapter(self.store).prepare(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock))
        adapter = Adapter(self.store, self.members, clock_ms=self.clock)
        result = adapter.request(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock))
        self.assertEqual(result["environment"]["state"], "QUARANTINED")
        self.assertFalse(result["connectable"])

    def test_four_slots_reject_fifth(self):
        bindings = [EditorBinding(team_id=1, project_id=1, user_id=i) for i in range(1, 6)]
        self.adapter.authorizer = Members(bindings)
        for b in bindings[:4]:
            self.start(b)
        with self.assertRaises(Denied):
            self.start(bindings[4])
        self.assertEqual(len(self.executor.rows), 4)

    def test_cpu_memory_and_disk_reservations(self):
        for budget in (Budget(1, 16384, 500), Budget(8, 1024, 500), Budget(8, 16384, 35)):
            with self.subTest(budget=budget):
                store = Store.create_new(Path(self.temp.name) / str(uuid4()), budget)
                adapter = Adapter(store, self.members, self.executor, self.clock)
                with self.assertRaises(Denied):
                    adapter.prepare(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock))
        self.assertFalse(self.executor.calls)

    def test_storage_evidence_refuses_capacity_shortage(self):
        caps = replace(capabilities(self.binding, self.clock), storage_limit_gib=35)
        with self.assertRaises(Denied):
            self.adapter.prepare(1, self.binding, self.profile, uuid4(), caps)

    def test_missing_runtime_or_unenforced_host_features_deny(self):
        cases = [{"runtime": "runc"}, {"platform": "kvm"}, {"architecture": "unknown"}, {"caller_uid": 0},
                 {"runtime_digest": "latest"}, {"image_digest": "sha256:" + "0" * 64},
                 {"sampled_ms": self.clock() - 5001}, {"sampled_ms": self.clock() + 1}]
        for field in ("rootless_chain", "caller_userns", "cgroup_v2_delegated", "cpu_memory_pids_enforced", "disk_quota_enforced", "network_namespace_isolated", "storage_healthy"):
            cases.append({field: False})
        for change in cases:
            with self.subTest(change=change), self.assertRaises(Denied):
                self.adapter.prepare(1, self.binding, self.profile, uuid4(), replace(capabilities(self.binding, self.clock), **change))
        self.assertIsNone(self.adapter.state(1, self.binding))

    def test_policy_rejects_unsafe_mutations_and_unknown_options(self):
        caps = capabilities(self.binding, self.clock)
        base = plan_for(self.binding, self.profile, caps.image_digest)
        changes = {"runtime": "runc", "fallback": True, "user": "0:0", "privileged": True,
                   "cap_drop": [], "no_new_privileges": False, "read_only_root": False,
                   "network": "host", "publish": [8080], "devices": ["/dev/kvm"], "host_namespaces": ["pid"],
                   "cpu_millis": 999999, "memory_bytes": 999999999999, "pids_limit": 999999,
                   "data_quota_gib": 999, "launch_enabled": True, "swap_bytes": -1,
                   "entrypoint": ["code-server", "--auth=none"], "unrecognized": True}
        for key, value in changes.items():
            plan = deepcopy(base)
            plan[key] = value
            with self.subTest(key=key), self.assertRaises(Denied):
                validate_plan(plan, self.binding, self.profile, caps.image_digest)
        for source in ("/", "/var/run/docker.sock", "/home/other/.ssh", "/project_storage", "OWNER_DATA:other", "../../secret"):
            plan = deepcopy(base)
            plan["mounts"][0]["source"] = source
            with self.subTest(source=source), self.assertRaises(Denied):
                validate_plan(plan, self.binding, self.profile, caps.image_digest)

    def test_stop_requires_fresh_complete_quiescent_save(self):
        self.start()
        for change in ({"jobs": 1}, {"jobs": None}, {"dirty": True}, {"dirty": None}, {"flushed": False},
                       {"generation": 0}, {"sampled_ms": self.clock() - 5001}, {"revision": 0}):
            with self.subTest(change=change), self.assertRaises(Denied):
                self.adapter.prepare(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock), self.proof(**change))
        self.assertEqual(self.adapter.state(1, self.binding)["environment"]["state"], "RUNNING")

    def test_save_failure_quarantines_without_deleting_or_releasing(self):
        self.start()
        proof = self.proof()
        self.executor.save_ok = False
        result = self.adapter.request(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock), proof)
        self.assertEqual(result["environment"]["state"], "QUARANTINED")
        self.assertEqual(self.executor.rows[identities(self.binding)[0]]["state"], "RUNNING")
        with self.store.transaction(self.clock()) as state:
            self.assertEqual(self.adapter.checked(state), (2, 2048, 36, 1))

    def test_lost_start_reply_never_creates_replacement(self):
        self.executor.lost_reply = True
        op = uuid4()
        result = self.start(operation=op)
        self.assertEqual(result["operation"]["status"], "UNKNOWN")
        self.start(operation=op)
        with self.assertRaises(Denied):
            self.start()
        self.assertEqual(len(self.executor.rows), 1)
        self.assertEqual(len(self.executor.calls), 1)

    def test_lost_stop_reply_holds_slot_and_data(self):
        self.start()
        self.executor.lost_reply = True
        result = self.adapter.request(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock), self.proof())
        self.assertEqual(result["environment"]["state"], "QUARANTINED")
        self.assertTrue(self.executor.rows[identities(self.binding)[0]]["data"])
        with self.store.transaction(self.clock()) as state:
            self.assertEqual(self.adapter.checked(state)[3], 1)

    def test_process_restart_and_expired_lease_are_fail_closed(self):
        ticket, _ = self.adapter.prepare(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock))
        restored = Adapter(Store(self.path), self.members, self.executor, self.clock)
        self.assertEqual(restored.state(1, self.binding)["environment"]["state"], "STARTING")
        self.clock.now += 30000
        self.assertEqual(restored.state(1, self.binding)["environment"]["state"], "QUARANTINED")
        with self.assertRaises(Denied):
            restored.dispatch(1, ticket)
        with self.assertRaises(Denied):
            self.start()
        self.assertFalse(self.executor.calls)

    def test_stale_or_forged_fence_cannot_complete(self):
        ticket, _ = self.adapter.prepare(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock))
        for changed in (replace(ticket, generation=2), replace(ticket, lease_ms=ticket.lease_ms + 1)):
            with self.assertRaises(Denied):
                self.adapter.dispatch(1, changed)
        self.adapter.dispatch(1, ticket)
        with self.assertRaises(Denied):
            self.adapter.dispatch(1, ticket)

    def test_inspect_owner_process_runtime_and_limits_mismatch(self):
        for field, value in (("process_uid", 0), ("caller_uid", 0), ("limits_enforced", False),
                             ("runtime_digest", "sha256:" + "3" * 64), ("volume_id", "other"), ("sandbox_id", "other"),
                             ("binding", EditorBinding(team_id=1, project_id=2, user_id=1))):
            with self.subTest(field=field):
                b = EditorBinding(team_id=1, project_id=1, user_id=10 + len(self.executor.rows))
                self.adapter.authorizer.bindings.add(tuple(b.model_dump().values()))
                ticket, _ = self.adapter.prepare(b.user_id, b, self.profile, uuid4(), capabilities(b, self.clock))
                observation = self.executor.execute(ticket, plan_for(b, self.profile, capabilities(b, self.clock).image_digest))
                result = self.adapter.finish(b.user_id, ticket, replace(observation, **{field: value}))
                self.assertEqual(result["environment"]["state"], "QUARANTINED")
                # 独立試験用に予算を使い切らないよう、新しい台帳へ切り替える。実リソースはない。
                store = Store.create_new(Path(self.temp.name) / str(uuid4()), Budget(8, 16384, 500))
                self.adapter = Adapter(store, self.adapter.authorizer, self.executor, self.clock)

    def test_inspect_configuration_downgrade_quarantines(self):
        ticket, _ = self.adapter.prepare(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock))
        observation = self.executor.execute(ticket, plan_for(self.binding, self.profile, capabilities(self.binding, self.clock).image_digest))
        observation.configuration["runtime"] = "runc"
        self.assertEqual(self.adapter.finish(1, ticket, observation)["environment"]["state"], "QUARANTINED")

    def test_duplicate_host_principal_and_owner_mismatch_deny(self):
        self.start()
        b = EditorBinding(team_id=1, project_id=1, user_id=2)
        self.members.bindings.add(tuple(b.model_dump().values()))
        caps = replace(capabilities(b, self.clock), caller_uid=capabilities(self.binding, self.clock).caller_uid)
        with self.assertRaises(Denied):
            self.adapter.prepare(2, b, self.profile, uuid4(), caps)
        row = self.executor.rows[identities(self.binding)[0]]
        row["binding"] = b
        result = self.adapter.request(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock), self.proof())
        self.assertEqual(result["environment"]["state"], "QUARANTINED")
        self.assertEqual(row["data"], "synthetic-edit-data")

    def test_database_failure_before_dispatch_does_not_execute(self):
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER reject_write BEFORE UPDATE ON snapshot BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECRET'); END")
        with self.assertRaisesRegex(Denied, "^gvisor_lab_denied$"):
            self.start()
        self.assertFalse(self.executor.calls)

    def test_database_failure_after_effect_retains_intent_then_quarantines(self):
        ticket, _ = self.adapter.prepare(1, self.binding, self.profile, uuid4(), capabilities(self.binding, self.clock))
        observation = self.executor.execute(ticket, plan_for(self.binding, self.profile, capabilities(self.binding, self.clock).image_digest))
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER reject_write BEFORE UPDATE ON snapshot BEGIN SELECT RAISE(ABORT,'SYNTHETIC_SECRET'); END")
        with self.assertRaisesRegex(Denied, "^gvisor_lab_denied$"):
            self.adapter.finish(1, ticket, observation)
        with self.store.connection() as db:
            db.execute("DROP TRIGGER reject_write")
        self.clock.now += 30000
        self.assertEqual(self.adapter.state(1, self.binding)["environment"]["state"], "QUARANTINED")
        self.assertTrue(self.executor.rows[identities(self.binding)[0]]["data"])

    def test_store_never_overwrites_existing_file(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(Denied, "^gvisor_lab_denied$"):
            Store.create_new(self.path, self.store.budget)
        self.assertEqual(self.path.read_bytes(), before)

    def test_mock_network_deny_is_not_real_packet_test(self):
        for target in ("169.254.169.254", "fd00:ec2::254", "postgres:5432", "shared-git", "other-sandbox", "internet"):
            with self.subTest(target=target), self.assertRaises(Denied):
                self.executor.connect(self.binding, target)

    def test_multiprocess_global_limit(self):
        self.compete(list(range(1, 9)), 4)

    def test_multiprocess_same_owner_single_reservation(self):
        self.compete([1] * 6, 1)

    def compete(self, users, expected):
        """spawnした試験プロセスだけを終了管理し、独立接続でSQLite予約を競合させる。"""
        ctx = multiprocessing.get_context("spawn")
        barrier, queue = ctx.Barrier(len(users)), ctx.Queue()
        processes = [ctx.Process(target=competing_prepare, args=(str(self.path), user, str(uuid4()), barrier, queue)) for user in users]
        try:
            for process in processes:
                process.start()
            results = [queue.get(timeout=45) for _ in processes]
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(results.count("reserved"), expected)
            self.assertEqual(results.count("gvisor_lab_denied"), len(users) - expected)
            with self.store.transaction(self.clock()) as data:
                self.assertEqual(self.adapter.checked(data)[3], expected)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()  # 自分が作った合成Python子プロセスだけ。VM/コンテナ操作ではない。
                process.join(5)
            queue.close()


class GvisorStaticTests(unittest.TestCase):
    """ファイル検査だけで無効ゲート・レシピ改変・秘密情報の非反射を確認する。"""

    def test_static_report_does_not_claim_runtime_verification(self):
        result = evaluate()
        self.assertEqual(result["status"], "STATIC_CONSISTENT_ONLY")
        self.assertTrue(result["runtime_source_unchanged"])
        for field in ("launch_enabled", "deployment_allowed", "connectable"):
            self.assertIs(result[field], False)
        for field in ("image_build", "real_runsc", "browser"):
            self.assertEqual(result[field], "NOT_RUN")

    def test_tampered_recipe_and_false_manifest_are_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "artifacts/gvisor-editor", root / "artifacts/gvisor-editor")
            (root / "backend/app").mkdir(parents=True)
            for name in ("editor_api.py", "editor_contracts.py", "editor_access.py"):
                shutil.copyfile(ROOT / "backend/app" / name, root / "backend/app" / name)
            recipe = root / "artifacts/gvisor-editor/Dockerfile"
            original = recipe.read_bytes()
            recipe.write_bytes(original + b"\nUSER root\nEXPOSE 8080\n")
            self.assertEqual(evaluate(root)["status"], "DENIED")
            recipe.write_bytes(original)
            manifest = root / "artifacts/gvisor-editor/manifest.example.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["runtime_test"] = "PASS"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(evaluate(root)["status"], "DENIED")

    def test_cli_unknown_arguments_do_not_echo_secrets(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(main(["--run", "SYNTHETIC_SECRET"]), 2)
        self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())

    def test_image_entrypoint_always_denies_without_launching_editor(self):
        path = ROOT / "artifacts/gvisor-editor/disabled_entrypoint.py"
        spec = importlib.util.spec_from_file_location("disabled_gvisor_image", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        with redirect_stderr(output), patch("subprocess.run", side_effect=AssertionError("must not execute")):
            self.assertEqual(module.main(), 78)
        self.assertEqual(output.getvalue().strip(), "gvisor_lab_runtime_disabled")
