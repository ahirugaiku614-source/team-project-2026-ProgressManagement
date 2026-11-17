"""第8段階の設計だけを、一時JSON・ソースコピー・モックで検査する。

実基盤、SDK、Docker、libvirt、SSH、DNS、DB、証明書へ問い合わせない。
全PASS申告でも実行許可にならないことと、第7段階のソース不変を回帰で守る。
"""

import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import editor_foundation_check as checker


class EditorFoundationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        source = checker.ROOT / "docs/editor-foundation"
        self.plan = json.loads((source / "plan.example.json").read_text(encoding="utf-8"))
        self.evidence = json.loads((source / "evidence.example.json").read_text(encoding="utf-8"))
        self.plan_path = self.directory / "plan.json"
        self.evidence_path = self.directory / "evidence.json"

    def evaluate(self, plan=None, evidence=None, root=checker.ROOT):
        self.plan_path.write_text(json.dumps(self.plan if plan is None else plan), encoding="utf-8")
        self.evidence_path.write_text(json.dumps(self.evidence if evidence is None else evidence), encoding="utf-8")
        return checker.evaluate(self.plan_path, self.evidence_path, root=root)

    def assert_disabled(self, result):
        self.assertIs(result["editor_launch_enabled"], False)
        self.assertIs(result["deployment_allowed"], False)
        self.assertIs(result["real_infrastructure_verified"], False)

    def test_example_is_consistent_but_all_real_checks_are_unrun(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "design_consistent")
        self.assertEqual(result["evidence_claims"], {"NOT_RUN": len(checker.CHECK_IDS), "PASS": 0, "FAIL": 0})
        self.assertTrue(result["runtime_source_unchanged"])
        self.assert_disabled(result)

    def test_all_pass_claims_still_do_not_enable_deployment(self):
        for key in self.evidence["checks"]:
            self.evidence["checks"][key] = {"status": "PASS", "evidence_id": "TEST-" + key.upper()}
        result = self.evaluate()
        self.assertEqual(result["evidence_claims"]["PASS"], len(checker.CHECK_IDS))
        self.assertEqual(result["status"], "design_consistent")
        self.assert_disabled(result)

    def test_failed_claim_is_a_stop_condition(self):
        self.evidence["checks"]["storage_boundary"] = {"status": "FAIL", "evidence_id": "TEST-STORAGE-1"}
        result = self.evaluate()
        self.assertEqual(result["status"], "evidence_failed")
        self.assert_disabled(result)

    def test_unknown_missing_or_secret_fields_are_rejected_without_echo(self):
        for key, value in (("private_token", "DO-NOT-ECHO"), ("worker_url", "https://DO-NOT-ECHO"), ("docker_socket", "/DO-NOT-ECHO")):
            plan = copy.deepcopy(self.plan)
            plan[key] = value
            result = self.evaluate(plan=plan)
            self.assertEqual(result["status"], "invalid_design")
            self.assertNotIn("DO-NOT-ECHO", json.dumps(result))
            self.assert_disabled(result)
        plan = copy.deepcopy(self.plan)
        del plan["storage"]
        self.assertEqual(self.evaluate(plan=plan)["status"], "invalid_design")

    def test_actual_endpoints_or_shared_cookie_domain_are_not_design_examples(self):
        for changes in ({"app_origin": "https://real.invalid"}, {"editor_suffix": "app.example.com"},
                        {"dns_address_example": "10.0.0.1"}, {"cookie_domain": ".example.com"},
                        {"cookie_secure": False}, {"cookie_http_only": False}, {"ws_recheck_seconds": 999},
                        {"session_max_seconds": 999}, {"strip_upstream_credentials": False}):
            with self.subTest(changes=changes):
                plan = copy.deepcopy(self.plan)
                plan["https"].update(changes)
                self.assertEqual(self.evaluate(plan=plan)["status"], "invalid_design")

    def test_guest_privileges_secrets_host_shares_and_legacy_access_are_rejected(self):
        for section, key, value in (("guest", "non_root", False), ("guest", "host_shares", True),
                                    ("guest", "docker_socket", True), ("guest", "app_credentials", True),
                                    ("guest", "dedicated_kernel", False), ("guest", "public_ports", True),
                                    ("storage", "delete_on_stop", True), ("storage", "legacy_access", True)):
            with self.subTest(key=key):
                plan = copy.deepcopy(self.plan)
                plan[section][key] = value
                self.assertEqual(self.evaluate(plan=plan)["status"], "invalid_design")

    def test_launch_ready_and_alternative_runtime_are_not_silently_accepted(self):
        for key, value in (("launch_enabled", True), ("infrastructure_prepared", True),
                           ("mode", "DEPLOY"), ("isolation", "DOCKER_DESKTOP"), ("version", True)):
            plan = copy.deepcopy(self.plan)
            plan[key] = value
            result = self.evaluate(plan=plan)
            self.assertEqual(result["status"], "invalid_design")
            self.assert_disabled(result)

    def test_guest_to_database_manager_or_other_vm_channel_is_rejected(self):
        for target, port in (("postgresql", 5432), ("manager", 8443), ("other_vm", 8443), ("internet", 443)):
            plan = copy.deepcopy(self.plan)
            plan["network"]["service_channels"][0] = {"source": "editor_vm", "target": target, "protocol": "mtls", "port": port}
            self.assertEqual(self.evaluate(plan=plan)["status"], "invalid_design")

    def test_network_must_be_enforced_outside_guest_and_cover_metadata_and_ipv6(self):
        for key, value in (("enforcement", "GUEST_FIREWALL_ONLY"), ("guest_new_egress", "ALLOW"),
                           ("ipv6", "UNRESTRICTED"), ("metadata", "ALLOW")):
            plan = copy.deepcopy(self.plan)
            plan["network"][key] = value
            self.assertEqual(self.evaluate(plan=plan)["status"], "invalid_design")

    def test_duplicate_or_unknown_network_channels_are_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["network"]["service_channels"][0] = plan["network"]["service_channels"][1]
        self.assertEqual(self.evaluate(plan=plan)["status"], "invalid_design")
        plan = copy.deepcopy(self.plan)
        plan["network"]["service_channels"][0]["port"] = True
        self.assertEqual(self.evaluate(plan=plan)["status"], "invalid_design")

    def test_cpu_memory_and_disk_overcommit_are_rejected(self):
        for key, value in (("host_vcpu", 1), ("host_memory_mib", 1024), ("host_disk_gib", 20), ("max_running", 17),
                           ("max_retained", 6), ("max_retained", 3)):
            plan = copy.deepcopy(self.plan)
            plan["capacity"][key] = value
            self.assertEqual(self.evaluate(plan=plan)["status"], "invalid_design")
        # 起動は1 VMでも停止中を含む6環境のディスクが必要なので、同じ512 GiBでは拒否する。
        plan = copy.deepcopy(self.plan)
        plan["capacity"].update(max_running=1, max_retained=6)
        self.assertEqual(self.evaluate(plan=plan)["errors"], ["capacity_overcommitted"])

    def test_capacity_requires_positive_integers_not_booleans(self):
        for value in (True, "4", 4.5, 0, -1):
            plan = copy.deepcopy(self.plan)
            plan["capacity"]["max_running"] = value
            self.assertEqual(self.evaluate(plan=plan)["status"], "invalid_design")

    def test_evidence_cannot_omit_checks_or_claim_pass_without_reference(self):
        for value in ({"status": "PASS", "evidence_id": None}, {"status": "READY", "evidence_id": None},
                      {"status": "NOT_RUN", "evidence_id": "TEST-UNRUN"},
                      {"status": "PASS", "evidence_id": "https://private-token.invalid"}):
            evidence = copy.deepcopy(self.evidence)
            evidence["checks"]["ws_revocation"] = value
            self.assertEqual(self.evaluate(evidence=evidence)["status"], "invalid_design")
        del self.evidence["checks"]["ws_revocation"]
        self.assertEqual(self.evaluate()["status"], "invalid_design")

    def test_duplicate_json_keys_are_rejected(self):
        self.plan_path.write_text('{"launch_enabled": false, "launch_enabled": true}')
        self.evidence_path.write_text(json.dumps(self.evidence))
        self.assertEqual(checker.evaluate(self.plan_path, self.evidence_path)["errors"], ["duplicate_json_key"])

    def test_non_json_numbers_large_and_missing_files_fail_without_private_details(self):
        self.evidence_path.write_text(json.dumps(self.evidence))
        for content in ("NaN", "DO-NOT-ECHO", "a" * (checker.MAX_BYTES + 1)):
            self.plan_path.write_text(content)
            result = checker.evaluate(self.plan_path, self.evidence_path)
            self.assertEqual(result["status"], "invalid_design")
            self.assertNotIn("DO-NOT-ECHO", json.dumps(result))
        self.assertEqual(checker.evaluate(self.directory / "DO-NOT-ECHO", self.evidence_path)["errors"], ["input_unavailable"])

    def test_stage7_gate_mutation_is_detected_without_importing_or_running_it(self):
        destination = self.directory / "backend/app"
        destination.mkdir(parents=True)
        for name in checker.RUNTIME_HASHES:
            content = (checker.ROOT / "backend/app" / name).read_bytes().replace(b"\r\n", b"\n")
            (destination / name).write_bytes(content.replace(b"\n", b"\r\n"))
        self.assertTrue(self.evaluate(root=self.directory)["runtime_source_unchanged"])
        path = destination / "editor_contracts.py"
        path.write_bytes(path.read_bytes().replace(b"raise EditorRuntimeUnavailable()", b"return None"))
        result = self.evaluate(root=self.directory)
        self.assertEqual(result["errors"], ["stage7_source_changed"])
        self.assert_disabled(result)

    def test_cli_has_no_network_or_process_side_effect_and_no_enable_flags(self):
        self.evaluate()
        with patch.object(subprocess, "run", side_effect=AssertionError("No external commands")), \
             patch("socket.socket", side_effect=AssertionError("No network")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            code = checker.main(["--plan", str(self.plan_path), "--evidence", str(self.evidence_path)])
        self.assertEqual(code, 0)
        self.assert_disabled(json.loads(output.getvalue()))

    def test_cli_rejects_enable_options_without_echoing_values(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO) as errors:
            code = checker.main(["--enable", "DO-NOT-ECHO"])
        self.assertEqual(code, 2)
        self.assertNotIn("DO-NOT-ECHO", output.getvalue() + errors.getvalue())
        self.assert_disabled(json.loads(output.getvalue()))
