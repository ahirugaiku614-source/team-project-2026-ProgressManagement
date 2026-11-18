"""実基盤へ接続せず、一時ファイル・永続モック・専用子プロセスだけで安全性を検証する。"""

from dataclasses import replace
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from app.editor_contracts import EditorBinding, EditorRuntimeUnavailable, require_editor_runtime
from vm_manager.contracts import CapacityExceeded, ClockRegression, Conflict, Forbidden, OwnershipMismatch, RegistryUnavailable, StaleFence
from vm_manager.manager import VMManager
from vm_manager.registry import SQLiteRegistry
from vm_manager_fakes import Clock, LostReply, Members, PersistentFakeDriver, concurrent_start, crash_start, database


class VMManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vm-manager-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = Clock()
        self.bindings = [EditorBinding(team_id=1, project_id=1, user_id=i) for i in range(1, 9)]
        self.binding = self.bindings[0]
        self.members = Members(self.bindings)
        self.registry = SQLiteRegistry.create_new(self.root / "registry.sqlite")
        self.driver = PersistentFakeDriver.create_new(self.root / "driver.sqlite", self.clock)
        self.manager = VMManager(self.registry, self.members, self.driver, self.clock, 1000)

    def request(self, kind="ENSURE_STARTED", binding=None, operation_id=None):
        binding = binding or self.binding
        return self.manager.request(binding.user_id, binding, kind, operation_id or uuid4())

    def test_same_operation_does_not_duplicate_start(self):
        operation_id = uuid4()
        first = self.request(operation_id=operation_id)
        second = self.request(operation_id=operation_id)
        self.assertEqual(first, second)
        self.assertEqual(len(self.driver.rows()), 1)
        with database(self.driver.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_distinct_operation_same_environment_is_noop(self):
        self.request()
        self.request()
        with database(self.driver.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_fifth_start_refused(self):
        for binding in self.bindings[:4]:
            self.request(binding=binding)
        with self.assertRaises(CapacityExceeded):
            self.request(binding=self.bindings[4])
        self.assertEqual(len(self.driver.rows()), 4)
        self.assertEqual(len(self.registry.snapshot()["environments"]), 4)

    def test_eight_members_keep_data_with_four_active_limit(self):
        for binding in self.bindings:
            self.request(binding=binding)
            self.request("STOP", binding)
        self.assertEqual(len(self.driver.rows()), 8)
        self.assertTrue(all(row["content"] == "synthetic-edit-data" for row in self.driver.rows()))

    def test_same_user_other_project_has_separate_vm_and_global_limit(self):
        other = EditorBinding(team_id=1, project_id=2, user_id=1)
        self.members.bindings.add((1, 2, 1))
        first, second = self.request(), self.request(binding=other)
        self.assertNotEqual(first["environment"]["vm_id"], second["environment"]["vm_id"])
        self.assertNotEqual(first["environment"]["disk_id"], second["environment"]["disk_id"])
        for binding in self.bindings[1:3]:
            self.request(binding=binding)
        with self.assertRaises(CapacityExceeded):
            self.request(binding=self.bindings[3])

    def test_stop_restart_retains_disk_and_content(self):
        first = self.request()
        self.request("STOP")
        self.assertEqual(self.driver.rows()[0]["content"], "synthetic-edit-data")
        self.assertEqual(self.request()["environment"]["disk_id"], first["environment"]["disk_id"])
        self.assertEqual(len(self.driver.rows()), 1)

    def test_historical_start_does_not_report_current_vm_running(self):
        operation_id = uuid4()
        self.request(operation_id=operation_id)
        self.request("STOP")
        result = self.request(operation_id=operation_id)
        self.assertEqual(result["operation"]["result_state"], "RUNNING")
        self.assertEqual(result["environment"]["state"], "STOPPED")

    def test_operation_id_cannot_change_owner_or_kind(self):
        operation_id = uuid4()
        self.request(operation_id=operation_id)
        for kind, binding in (("STOP", self.binding), ("ENSURE_STARTED", self.bindings[1])):
            with self.assertRaises(Conflict):
                self.request(kind, binding, operation_id)

    def test_owner_and_current_membership_required(self):
        with self.assertRaises(Forbidden):
            self.manager.request(2, self.binding, "STOP", uuid4())
        self.members.bindings.clear()
        with self.assertRaises(Forbidden):
            self.manager.state(1, self.binding)
        with self.assertRaises(Forbidden):
            self.request()
        self.assertEqual(self.registry.snapshot()["environments"], [])

    def test_project_team_mismatch_rejected(self):
        self.request()
        other = EditorBinding(team_id=2, project_id=1, user_id=1)
        self.members.bindings.add((2, 1, 1))
        with self.assertRaises(OwnershipMismatch):
            self.request(binding=other)

    def test_authorization_loss_before_dispatch_does_not_call_driver(self):
        fence, _ = self.manager.prepare(1, self.binding, "ENSURE_STARTED", uuid4())
        self.members.bindings.clear()
        with self.assertRaises(Forbidden):
            self.manager.dispatch(1, fence)
        self.assertEqual(self.driver.rows(), [])

    def test_lost_reply_quarantines_and_observes_without_recreating(self):
        self.manager.driver = LostReply(self.driver)
        result = self.request()
        self.assertEqual(result["environment"]["state"], "QUARANTINED")
        self.assertNotIn("synthetic-secret", str(result))
        with self.assertRaises(Conflict):
            self.request()
        self.manager.driver = self.driver
        observed = self.request("GET_STATE")
        self.assertEqual(observed["environment"]["state"], "RUNNING")
        self.assertEqual(len(self.driver.rows()), 1)

    def test_stop_failure_keeps_slot_and_disk(self):
        self.request()
        self.manager.driver = LostReply(self.driver)
        self.assertEqual(self.request("STOP")["environment"]["state"], "QUARANTINED")
        for binding in self.bindings[1:4]:
            self.request(binding=binding)
        with self.assertRaises(CapacityExceeded):
            self.request(binding=self.bindings[4])
        self.manager.driver = self.driver
        self.assertEqual(self.request("GET_STATE")["environment"]["state"], "STOPPED")
        self.request(binding=self.bindings[4])
        self.assertEqual(self.driver.rows()[0]["content"], "synthetic-edit-data")

    def test_stop_failure_before_effect_does_not_report_stopped(self):
        self.request()
        with patch.object(self.driver, "execute", side_effect=TimeoutError("synthetic")):
            result = self.request("STOP")
        self.assertEqual(result["environment"]["state"], "QUARANTINED")
        self.assertEqual(self.driver.rows()[0]["vm_state"], "RUNNING")
        self.assertEqual(self.request("GET_STATE")["environment"]["state"], "RUNNING")

    def test_authorization_loss_after_effect_holds_slot(self):
        fence, _ = self.manager.prepare(1, self.binding, "ENSURE_STARTED", uuid4())
        observation = self.driver.execute(fence)
        self.members.bindings.clear()
        with self.assertRaises(Forbidden):
            self.manager.complete(1, fence, observation)
        self.assertEqual(self.registry.snapshot()["environments"][0]["error_code"], "authorization_lost")
        self.assertEqual(len(self.driver.rows()), 1)

    def test_blocked_old_dispatch_is_fenced_after_other_manager_observes(self):
        fence, _ = self.manager.prepare(1, self.binding, "ENSURE_STARTED", uuid4())
        # 外部処理直前で待たされている旧要求を模擬する。別プロセス相当の管理者が先に照合する。
        self.manager.mark_unknown(fence, "driver_uncertain")
        other = VMManager(SQLiteRegistry(self.registry.path), self.members, self.driver, self.clock, 1000)
        self.assertEqual(other.request(1, self.binding, "GET_STATE", uuid4())["environment"]["state"], "ABSENT")
        with self.assertRaises(StaleFence):
            self.driver.execute(fence)
        self.assertEqual(self.driver.rows(), [])

    def test_active_different_operation_cannot_steal_reservation(self):
        self.manager.prepare(1, self.binding, "ENSURE_STARTED", uuid4())
        with self.assertRaises(Conflict):
            self.request()
        with self.assertRaises(Conflict):
            self.request("STOP")
        with self.assertRaises(Conflict):
            self.request("GET_STATE")
        self.assertEqual(len(self.registry.snapshot()["operations"]), 1)

    def test_timeout_before_effect_keeps_slot_until_fenced_observation(self):
        fence, _ = self.manager.prepare(1, self.binding, "ENSURE_STARTED", uuid4())
        self.clock.now += 1001
        self.assertEqual(self.manager.state(1, self.binding)["state"], "QUARANTINED")
        self.assertEqual(self.request("GET_STATE")["environment"]["state"], "ABSENT")
        with self.assertRaises(StaleFence):
            self.driver.execute(fence)

    def test_old_generation_rejected_at_driver_and_completion(self):
        fence, _ = self.manager.prepare(1, self.binding, "ENSURE_STARTED", uuid4())
        observation = self.driver.execute(fence)
        self.manager.mark_unknown(fence, "driver_uncertain")
        self.request("GET_STATE")
        with self.assertRaises(StaleFence):
            self.driver.execute(fence)
        with self.assertRaises(StaleFence):
            self.manager.complete(1, fence, observation)

    def test_delayed_response_after_lease_is_not_success(self):
        fence, _ = self.manager.prepare(1, self.binding, "ENSURE_STARTED", uuid4())
        observation = self.driver.execute(fence)
        self.clock.now += 1000
        result = self.manager.complete(1, fence, observation)
        self.assertEqual(result["operation"]["status"], "UNKNOWN")
        self.assertEqual(len(self.driver.rows()), 1)

    def test_forged_extended_lease_rejected(self):
        fence, _ = self.manager.prepare(1, self.binding, "ENSURE_STARTED", uuid4())
        observation = self.driver.execute(fence)
        with self.assertRaises(StaleFence):
            self.manager.complete(1, replace(fence, lease_until_ms=fence.lease_until_ms + 1), observation)

    def test_vm_and_disk_ownership_mismatch_quarantines_without_mutation(self):
        self.request()
        with database(self.driver.path) as db:
            db.execute("UPDATE resources SET owner=?", (self.bindings[1].model_dump_json(),))
        before = self.driver.rows()
        self.assertEqual(self.request("STOP")["environment"]["error_code"], "owner_mismatch")
        self.assertEqual(self.driver.rows(), before)

    def test_crosswired_disk_rejected_before_stop(self):
        self.request()
        with database(self.driver.path) as db:
            db.execute("UPDATE resources SET disk_id='another-owner-disk'")
        before = self.driver.rows()
        self.assertEqual(self.request("STOP")["environment"]["state"], "QUARANTINED")
        self.assertEqual(before, self.driver.rows())

    def test_vm_owner_matches_but_disk_owner_differs(self):
        self.request()
        with database(self.driver.path) as db:
            db.execute("UPDATE resources SET disk_owner=?", (self.bindings[1].model_dump_json(),))
        before = self.driver.rows()
        self.assertEqual(self.request("STOP")["environment"]["error_code"], "owner_mismatch")
        self.assertEqual(self.driver.rows(), before)

    def test_missing_known_resource_is_not_recreated(self):
        self.request()
        self.request("STOP")
        # 破損はモック行だけに注入する。実ファイル・実VM・実ディスクは存在しない。
        with database(self.driver.path) as db:
            db.execute("UPDATE resources SET vm_id='orphaned-test-resource',disk_id='orphaned-test-disk'")
        before = self.driver.rows()
        self.assertEqual(self.request()["environment"]["state"], "QUARANTINED")
        self.assertEqual(self.driver.rows(), before)

    def test_default_driver_and_old_runtime_gate_remain_disabled(self):
        self.manager.driver = VMManager(self.registry, self.members).driver
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), patch("subprocess.run", side_effect=AssertionError("CLI forbidden")):
            self.assertEqual(self.request()["environment"]["state"], "QUARANTINED")
            with self.assertRaises(EditorRuntimeUnavailable):
                require_editor_runtime()
        self.assertEqual(self.driver.rows(), [])

    def test_restart_restores_state_and_capacity(self):
        for binding in self.bindings[:4]:
            self.request(binding=binding)
        self.manager = VMManager(SQLiteRegistry(self.registry.path), self.members, PersistentFakeDriver(self.driver.path, self.clock), self.clock, 1000)
        self.assertEqual(self.manager.state(1, self.binding)["state"], "RUNNING")
        with self.assertRaises(CapacityExceeded):
            self.request(binding=self.bindings[4])

    def test_sql_failure_before_intent_calls_no_driver(self):
        with self.registry.connection() as db:
            db.execute("CREATE TRIGGER fail_intent BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(RegistryUnavailable):
            self.request()
        self.assertEqual(self.driver.rows(), [])
        self.assertEqual(self.registry.snapshot()["environments"], [])

    def test_db_failure_after_effect_keeps_intent_and_recovers(self):
        fence, _ = self.manager.prepare(1, self.binding, "ENSURE_STARTED", uuid4())
        observation = self.driver.execute(fence)
        with self.registry.connection() as db:
            db.execute("CREATE TRIGGER fail_completion BEFORE UPDATE OF status ON operations WHEN NEW.status='SUCCEEDED' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(RegistryUnavailable):
            self.manager.complete(1, fence, observation)
        self.assertEqual(len(self.driver.rows()), 1)
        self.assertEqual(self.registry.snapshot()["operations"][0]["status"], "ACTIVE")
        with self.registry.connection() as db:
            db.execute("DROP TRIGGER fail_completion")
        self.clock.now += 1001
        self.assertEqual(self.request("GET_STATE")["environment"]["state"], "RUNNING")

    def test_clock_rollback_stops_processing(self):
        self.request()
        self.clock.now -= 1
        with self.assertRaises(ClockRegression):
            self.request("STOP")
        self.assertEqual(self.driver.rows()[0]["vm_state"], "RUNNING")

    def test_new_registry_never_overwrites_existing_file(self):
        path = self.root / "unrelated"
        path.write_bytes(b"must-keep")
        with self.assertRaises(FileExistsError):
            SQLiteRegistry.create_new(path)
        self.assertEqual(path.read_bytes(), b"must-keep")
        with self.assertRaises(RegistryUnavailable):
            SQLiteRegistry(path)

    def test_registry_rejects_foreign_sqlite_and_hardlink(self):
        foreign = self.root / "foreign.sqlite"
        with database(foreign) as db:
            db.execute("CREATE TABLE other(id INTEGER)")
        with self.assertRaises(RegistryUnavailable):
            SQLiteRegistry(foreign)
        import os
        linked = self.root / "linked.sqlite"
        os.link(self.registry.path, linked)
        with self.assertRaises(RegistryUnavailable):
            SQLiteRegistry(linked)

    def test_transaction_rollback_releases_only_uncommitted_reservation(self):
        with self.assertRaises(RuntimeError):
            with self.registry.transaction(self.clock()) as db:
                db.execute("UPDATE meta SET max_running=1")
                raise RuntimeError("synthetic")
        with self.registry.connection() as db:
            self.assertEqual(db.execute("SELECT max_running FROM meta").fetchone()[0], 4)

    def run_children(self, users, same_operation=False):
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(len(users)), context.Queue()
        shared = str(uuid4())
        children = [context.Process(target=concurrent_start, args=(str(self.registry.path), self.driver.path, user,
                    shared if same_operation else str(uuid4()), barrier, queue)) for user in users]
        try:
            for child in children:
                child.start()
            results = [queue.get(timeout=40) for _ in children]
            for child in children:
                child.join(timeout=20)
                self.assertEqual(child.exitcode, 0)
            return results
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=10)
            queue.close()
            queue.join_thread()

    def test_multiple_processes_share_four_slot_limit(self):
        results = self.run_children(range(1, 9))
        self.assertEqual(sum(result[0] == "ok" for result in results), 4, results)
        self.assertEqual(sum(result[0] == "CapacityExceeded" for result in results), 4, results)
        self.assertEqual(len(self.driver.rows()), 4)

    def test_multiple_processes_duplicate_operation_is_one_start(self):
        results = self.run_children([1, 1, 1, 1], same_operation=True)
        self.assertTrue(all(result[0] == "ok" for result in results), results)
        self.assertEqual(len(self.driver.rows()), 1)
        with database(self.driver.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_multiple_processes_different_operations_same_owner(self):
        results = self.run_children([1, 1, 1, 1])
        self.assertTrue(all(result[0] in ("ok", "Conflict") for result in results), results)
        self.assertTrue(any(result[0] == "ok" for result in results))
        self.assertEqual(len(self.driver.rows()), 1)
        with database(self.driver.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def run_crash(self, after_effect):
        child = multiprocessing.get_context("spawn").Process(target=crash_start,
                    args=(str(self.registry.path), self.driver.path, after_effect))
        child.start()
        child.join(timeout=20)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
            self.fail("test child timed out")
        self.assertEqual(child.exitcode, 73)
        self.manager = VMManager(SQLiteRegistry(self.registry.path), self.members, self.driver, self.clock, 1000)
        operation = self.registry.snapshot()["operations"][0]
        # 再送では未実行と推測してdispatchしない。期限切れ後の新しい照合だけを許す。
        self.request(operation_id=UUID(operation["operation_id"]))
        self.clock.now += 1001
        return self.request("GET_STATE")

    def test_process_exit_after_preparation_recovers_absent(self):
        self.assertEqual(self.run_crash(False)["environment"]["state"], "ABSENT")
        self.assertEqual(self.driver.rows(), [])

    def test_process_exit_after_effect_recovers_running(self):
        self.assertEqual(self.run_crash(True)["environment"]["state"], "RUNNING")
        self.assertEqual(len(self.driver.rows()), 1)


if __name__ == "__main__":
    unittest.main()
