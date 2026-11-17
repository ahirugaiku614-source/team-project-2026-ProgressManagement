"""Dockerの応答消失・失敗を模擬し、実リソースを作らず保全と報告を検証する。"""

import json
import tempfile
import unittest

from tools.editor_lab_resources import LabResources, LabFailure, LABEL, report


class FakeDocker:
    def __init__(self):
        self.daemon = "test-daemon"
        self.objects = {("volume", "code_server_workspace"): {"Name": "code_server_workspace", "Labels": {}, "CreatedAt": "old"}}
        self.calls = []
        self.journal = None
        self.failure = None
        self.inspect_fails = False

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "info":
            if self.failure == "info-unavailable":
                raise TimeoutError("private daemon unreachable")
            return self.daemon
        if args[1:2] == ("ls",):
            pattern = args[args.index("--filter") + 1].removeprefix("name=^").removeprefix("/").removesuffix("$")
            return pattern if (args[0], pattern) in self.objects else ""
        if args[0:2] == ("volume", "create") or args[0] == "create":
            kind = "volume" if args[0] == "volume" else "container"
            name = args[-1] if kind == "volume" else args[args.index("--name") + 1]
            # 作成コマンドの呼出時点ですでに意図がディスクへ記録されている必要がある。
            saved = json.loads(self.journal.read_text())
            assert any(i["name"] == name and i["state"] == "PLANNED" for i in saved["resources"])
            if self.failure == "before-create":
                raise RuntimeError("private daemon details")
            label = args[args.index("--label") + 1].split("=", 1)[1]
            value = {"Name": name, "Labels": {LABEL: label}, "CreatedAt": "created-" + str(len(self.objects))} if kind == "volume" else {
                "Name": "/" + name, "Id": str(len(self.objects)).zfill(64), "Config": {"Labels": {LABEL: label}}, "State": {"Running": False}}
            self.objects[(kind, name)] = value
            if self.failure == "lost-create-reply":
                raise TimeoutError("private daemon endpoint")
            return name if kind == "volume" else value["Id"]
        if args[1:2] == ("inspect",):
            if self.inspect_fails:
                raise TimeoutError("private inspect details")
            key = args[0], args[2]
            if key not in self.objects:
                raise RuntimeError("not found or unavailable")
            return json.dumps([self.objects[key]])
        if args[1:2] == ("rm",):
            key = (args[0], args[2])
            if args[0] == "container":
                key = next(k for k, v in self.objects.items() if k[0] == "container" and v["Id"] == args[2])
            self.objects.pop(key)
            if self.failure == "lost-remove-reply":
                raise TimeoutError("private removal details")
            return args[2]
        raise AssertionError("Unexpected operation")


class EditorLabResourceFailureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fake = FakeDocker()
        self.lab = LabResources(self.fake, directory=directory.name, announce=False)
        self.fake.journal = self.lab.path

    def assert_no_removal(self):
        self.assertFalse(any(call[1:2] == ("rm",) for call in self.fake.calls))
        self.assertIn(("volume", "code_server_workspace"), self.fake.objects)

    def test_volume_created_but_reply_lost_is_recorded_and_never_automatically_deleted(self):
        self.fake.failure = "lost-create-reply"
        with self.assertRaises(LabFailure) as error:
            self.lab.volume("user-a")
        self.assertIn(str(self.lab.path), str(error.exception))
        self.assertNotIn("private", str(error.exception))
        saved = json.loads(self.lab.path.read_text())
        self.assertEqual(saved["resources"][0]["state"], "UNCERTAIN")
        self.assertEqual(report(self.lab.path, self.fake)["resources"][0]["observation"], "ownership_matches")
        with self.assertRaises(LabFailure):
            self.lab.cleanup()
        self.assert_no_removal()

    def test_container_reply_loss_preserves_the_related_confirmed_volume(self):
        volume = self.lab.volume("user-a")
        self.fake.failure = "lost-create-reply"
        with self.assertRaises(LabFailure):
            self.lab.container(["test-image"])
        with self.assertRaises(LabFailure):
            self.lab.cleanup()
        self.assertIn(("volume", volume), self.fake.objects)
        observations = report(self.lab.path, self.fake)
        self.assertEqual(len(observations["resources"]), 2)
        self.assertTrue(observations["read_only"])
        self.assert_no_removal()

    def test_failure_before_creation_is_not_misreported_as_confirmed_absence(self):
        self.fake.failure = "before-create"
        with self.assertRaises(LabFailure):
            self.lab.volume("user-a")
        self.assertEqual(report(self.lab.path, self.fake)["resources"][0]["observation"], "not_found_or_unavailable")
        self.assert_no_removal()

    def test_inspect_failure_after_creation_preserves_identifiable_intent(self):
        self.fake.inspect_fails = True
        with self.assertRaises(LabFailure):
            self.lab.volume("user-a")
        self.fake.inspect_fails = False
        self.assertEqual(report(self.lab.path, self.fake)["resources"][0]["observation"], "ownership_matches")
        with self.assertRaises(LabFailure):
            self.lab.cleanup()
        self.assert_no_removal()

    def test_existing_name_is_not_adopted_or_deleted(self):
        name = "editor-lab-" + self.lab.run_id + "-user-a"
        self.fake.objects[("volume", name)] = {"Name": name, "Labels": {}, "CreatedAt": "existing"}
        with self.assertRaises(LabFailure):
            self.lab.volume("user-a")
        self.assertEqual(report(self.lab.path, self.fake)["resources"][0]["observation"], "ownership_mismatch")
        self.assertFalse(any(c[:2] == ("volume", "create") for c in self.fake.calls))
        self.assert_no_removal()

    def test_ownership_change_prevents_all_cleanup(self):
        first = self.lab.volume("user-a")
        second = self.lab.volume("user-b")
        self.fake.objects[("volume", second)]["Labels"] = {LABEL: "another-run"}
        with self.assertRaises(LabFailure):
            self.lab.cleanup()
        self.assertIn(("volume", first), self.fake.objects)
        self.assert_no_removal()

    def test_recreated_same_named_volume_is_not_deleted(self):
        name = self.lab.volume("user-a")
        self.fake.objects[("volume", name)]["CreatedAt"] = "recreated-later"
        with self.assertRaises(LabFailure):
            self.lab.cleanup()
        self.assertEqual(report(self.lab.path, self.fake)["resources"][0]["observation"], "identity_mismatch")
        self.assert_no_removal()

    def test_changed_daemon_blocks_cleanup_and_cross_daemon_inspection(self):
        self.lab.volume("user-a")
        self.fake.daemon = "different-daemon"
        with self.assertRaises(LabFailure):
            self.lab.cleanup()
        before = len(self.fake.calls)
        self.assertEqual(report(self.lab.path, self.fake)["resources"][0]["observation"], "context_mismatch")
        self.assertTrue(all(c[0] == "info" for c in self.fake.calls[before:]))
        self.assert_no_removal()

    def test_offline_report_still_identifies_planned_resources_without_inspection(self):
        name = self.lab.volume("user-a")
        self.fake.failure = "info-unavailable"
        before = len(self.fake.calls)
        result = report(self.lab.path, self.fake)
        self.assertEqual(result["context_status"], "context_unavailable")
        self.assertEqual(result["resources"][0]["name"], name)
        self.assertEqual(result["resources"][0]["observation"], "context_unavailable")
        self.assertTrue(all(c[0] == "info" for c in self.fake.calls[before:]))
        self.assert_no_removal()

    def test_running_container_is_not_stopped_or_force_removed(self):
        self.lab.volume("user-a")
        name = self.lab.container(["test-image"])
        self.fake.objects[("container", name)]["State"]["Running"] = True
        with self.assertRaises(LabFailure):
            self.lab.cleanup()
        self.assert_no_removal()

    def test_successful_cleanup_removes_only_confirmed_objects_containers_first(self):
        self.lab.volume("user-a")
        self.lab.container(["test-image"])
        self.lab.cleanup()
        removals = [c for c in self.fake.calls if c[1:2] == ("rm",)]
        self.assertEqual([c[0] for c in removals], ["container", "volume"])
        self.assertIn(("volume", "code_server_workspace"), self.fake.objects)
        self.assertTrue(all(i["state"] == "REMOVED" for i in json.loads(self.lab.path.read_text())["resources"]))

    def test_removal_acknowledgement_loss_is_reported_without_guessing_or_pruning(self):
        self.lab.volume("user-a")
        self.lab.container(["test-image"])
        self.fake.failure = "lost-remove-reply"
        with self.assertRaises(LabFailure):
            self.lab.cleanup()
        self.assertEqual(report(self.lab.path, self.fake)["resources"][1]["observation"], "not_found_or_unavailable")
        self.assertFalse(any(c[0] in ("prune", "stop") or "--force" in c or "-f" in c for c in self.fake.calls))
        self.assertIn(("volume", "code_server_workspace"), self.fake.objects)
