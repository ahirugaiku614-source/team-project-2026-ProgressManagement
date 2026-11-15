import json
import subprocess
import unittest
from unittest.mock import patch

from tools.editor_preflight import ProbeUnavailable, collect_docker_info, evaluate_worker, main


class EditorPreflightTests(unittest.TestCase):
    def baseline(self):
        return {
            "OSType": "linux", "SecurityOptions": ["name=rootless", "name=seccomp,profile=builtin"],
            "Runtimes": {"runsc": {}}, "CgroupVersion": "2", "CgroupDriver": "systemd",
            "MemoryLimit": True, "PidsLimit": True, "CpuCfsQuota": True,
        }

    def test_baseline_still_requires_validation_and_never_enables_launch(self):
        report = evaluate_worker(self.baseline())
        self.assertEqual(report["status"], "requires_validation")
        self.assertFalse(report["editor_launch_enabled"])
        self.assertTrue(all(report["checks"].values()))

    def test_each_missing_prerequisite_blocks(self):
        for field in self.baseline():
            with self.subTest(field=field):
                info = self.baseline()
                info.pop(field)
                report = evaluate_worker(info)
                self.assertEqual(report["status"], "blocked")
                self.assertFalse(report["editor_launch_enabled"])

    def test_current_rootful_runc_configuration_is_blocked(self):
        info = self.baseline()
        info["SecurityOptions"] = ["name=seccomp,profile=builtin", "name=cgroupns"]
        info["Runtimes"] = {"runc": {}, "io.containerd.runc.v2": {}, "nvidia": {}}
        report = evaluate_worker(info)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["checks"]["rootless_engine"])
        self.assertFalse(report["checks"]["runsc_registered"])

    def test_other_engine_or_cgroup_configuration_is_blocked(self):
        for field, value in (("OSType", "windows"), ("CgroupVersion", "1"), ("CgroupDriver", "none")):
            with self.subTest(field=field):
                info = self.baseline()
                info[field] = value
                self.assertEqual(evaluate_worker(info)["status"], "blocked")

    def test_malformed_values_do_not_pass(self):
        for field, value in (("SecurityOptions", "name=rootless,name=seccomp"),
                             ("Runtimes", ["runsc"]), ("MemoryLimit", "true"),
                             ("PidsLimit", 1), ("CpuCfsQuota", "yes")):
            with self.subTest(field=field):
                info = self.baseline()
                info[field] = value
                self.assertEqual(evaluate_worker(info)["status"], "blocked")

    def test_substring_matches_cannot_satisfy_security_options(self):
        info = self.baseline()
        info["SecurityOptions"] = ["name=rootless-fake", "name=seccomp-disabled"]
        self.assertEqual(evaluate_worker(info)["status"], "blocked")

    def test_unconfined_or_unknown_seccomp_profile_is_blocked(self):
        for value in ("name=seccomp,profile=unconfined", "name=seccomp"):
            with self.subTest(value=value):
                info = self.baseline()
                info["SecurityOptions"] = ["name=rootless", value]
                self.assertEqual(evaluate_worker(info)["status"], "blocked")

    def test_no_host_metadata_or_secrets_are_reported(self):
        info = self.baseline()
        info.update(DockerRootDir="/private/host/path", Name="private-host", Secret="private-value")
        rendered = json.dumps(evaluate_worker(info))
        for value in ("/private/host/path", "private-host", "private-value", "DockerRootDir"):
            self.assertNotIn(value, rendered)

    def test_probe_only_runs_read_only_info_with_argument_array(self):
        with patch("tools.editor_preflight.subprocess.run") as run:
            run.return_value.stdout = json.dumps(self.baseline())
            self.assertEqual(collect_docker_info(), self.baseline())
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["docker", "info", "--format", "{{json .}}"])
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["timeout"], 15)
            run.assert_called_once()

    def test_probe_errors_are_sanitized(self):
        for error in (FileNotFoundError("secret"), subprocess.TimeoutExpired("secret", 15),
                      subprocess.CalledProcessError(1, "secret", stderr="private-daemon-endpoint")):
            with self.subTest(error=type(error).__name__):
                with patch("tools.editor_preflight.subprocess.run", side_effect=error):
                    with self.assertRaises(ProbeUnavailable) as raised:
                        collect_docker_info()
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("private-daemon-endpoint", str(raised.exception))

    def test_invalid_docker_json_is_rejected(self):
        for value in ("not-json", "[]", "null", '"text"'):
            with self.subTest(value=value):
                with patch("tools.editor_preflight.subprocess.run") as run:
                    run.return_value.stdout = value
                    with self.assertRaises(ProbeUnavailable):
                        collect_docker_info()

    def test_cli_reports_unavailability_without_enabling_launch(self):
        with patch("tools.editor_preflight.collect_docker_info", side_effect=ProbeUnavailable("Unavailable")), patch("builtins.print") as output:
            self.assertEqual(main(), 2)
            report = json.loads(output.call_args.args[0])
            self.assertEqual(report["status"], "unavailable")
            self.assertFalse(report["editor_launch_enabled"])
