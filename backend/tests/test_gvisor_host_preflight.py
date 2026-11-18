"""第14段階：合成Linux情報と一時ファイルだけで読取り専用チェックの境界を検証する。"""

import ast
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gvisor_host_preflight.check import RUNTIME_CHECKS, collect, main, subordinate
from gvisor_host_preflight.probe import LocalProbe, Unavailable, components, mount_records, open_readonly, read_text
from gvisor_lab.check import evaluate


class FakeProbe:
    """読取り済みと仮定する合成情報。実Linux調査済みの証拠には使用しない。"""

    def __init__(self):
        """各条件を差し替えられる架空hostを作る。秘密や実host情報は含まない。"""
        self.info = {"system": "Linux", "machine": "aarch64", "kernel": "6.8.0-lab", "uid": 1000}
        self.files = {"os_release": 'ID=ubuntu\nVERSION_ID="24.04"\n',
                      "passwd": "lab:x:1000:1000::/nonexistent:/bin/false\n",
                      "subuid": "lab:100000:65536\nother:200000:65536\n",
                      "subgid": "lab:100000:65536\nother:200000:65536\n",
                      "max_userns": "10000\n", "max_netns": "10000\n", "unprivileged_userns": "1\n", "apparmor_userns": "1\n",
                      "root_controllers": "cpu memory pids io\n", "current_controllers": "cpu memory pids\n",
                      "current_subtree": "cpu memory pids\n", "current_cpu": "max 100000\n", "current_memory": "max\n",
                      "current_pids": "max\n", "current_swap": "max\n"}
        self.files["mountinfo"] = "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
        self.image = {"architecture": "arm64", "sha256": "1" * 64, "executable_bit": True}
        self.disk = {"filesystem": "ext4", "total_bytes": 100 * 1024**3, "available_bytes": 60 * 1024**3,
                     "available_inodes": 100000, "quota_mount_option": True}

    def system(self):
        """合成unameとUIDを返す。"""
        return deepcopy(self.info)

    def text(self, key):
        """合成ファイルを返す。存在しない場合は欠測にする。"""
        return self.files[key]

    def namespace(self, kind):
        """namespace linkが見えた仮定だけを返す。unshare成功ではない。"""
        return True

    def mapping_helper(self, kind):
        """helper配置の仮定だけを返す。権限昇格処理は実行しない。"""
        return {"executable_bit": True, "setuid_bit": True, "root_owned": True}

    def binary(self, path):
        """ELF候補の観測結果を返す。実行済みという意味ではない。"""
        return deepcopy(self.image)

    def storage(self, path):
        """空き容量の合成値。quota強制の証明ではない。"""
        return deepcopy(self.disk)


class ReportTests(unittest.TestCase):
    """好条件でも実行不可であり、欠測・不適合・秘密情報を安全に扱うことを確認する。"""

    def setUp(self):
        """実hostに接続しないテスト境界を準備する。"""
        self.probe = FakeProbe()

    def collect(self):
        """fixtureであることを明記して合成調査を行う。"""
        return collect(self.probe, "/synthetic/runsc", "/synthetic/data", "1" * 64, synthetic=True)

    def test_complete_static_observations_never_allow_execution(self):
        result = self.collect()
        self.assertEqual(result["source"], "SYNTHETIC_MOCK")
        self.assertEqual(result["collection"], "COLLECTED")
        self.assertEqual(result["readiness"], "BLOCKED")
        self.assertEqual(set(result["runtime_checks"]), set(RUNTIME_CHECKS))
        self.assertTrue(all(value == "NOT_RUN" for value in result["runtime_checks"].values()))
        for key in ("launch_enabled", "deployment_allowed", "connectable"):
            self.assertIs(result[key], False)
        self.assertNotIn("PASS", json.dumps(result))

    def test_arm64_and_amd64_are_observations_not_compatibility_pass(self):
        for machine, arch in (("aarch64", "arm64"), ("x86_64", "amd64")):
            with self.subTest(machine=machine):
                self.probe.info["machine"] = machine
                self.probe.image["architecture"] = arch
                result = self.collect()
                self.assertEqual(result["facts"]["architecture"]["architecture"], arch)
                self.assertEqual(result["facts"]["architecture"]["runtime_compatibility"], "NOT_RUN")

    def test_unknown_architecture_is_fail(self):
        self.probe.info["machine"] = "mips"
        self.assertEqual(self.collect()["facts"]["architecture"]["status"], "FAIL")

    def test_non_linux_does_not_read_host_files(self):
        self.probe.info["system"] = "Windows"
        with patch.object(self.probe, "text", side_effect=AssertionError("no reads")) as reader:
            self.assertEqual(self.collect()["collection"], "NOT_RUN")
            reader.assert_not_called()

    def test_root_caller_is_fail_but_nonroot_is_not_process_chain_proof(self):
        self.assertEqual(self.collect()["facts"]["caller"]["entire_process_chain"], "NOT_RUN")
        self.probe.info["uid"] = 0
        self.assertEqual(self.collect()["facts"]["caller"]["status"], "FAIL")

    def test_namespace_zero_limits_and_disabled_clone_fail(self):
        for key in ("max_userns", "max_netns", "unprivileged_userns"):
            with self.subTest(key=key):
                self.probe.files[key] = "0\n"
                self.assertEqual(self.collect()["facts"][key]["status"], "FAIL")

    def test_missing_or_malformed_read_is_unknown_and_does_not_echo(self):
        self.probe.files["current_memory"] = "SYNTHETIC_SECRET"
        del self.probe.files["max_userns"]
        with patch.object(self.probe, "binary", side_effect=PermissionError("SYNTHETIC_SECRET")):
            result = self.collect()
        self.assertEqual(result["facts"]["current_memory"]["status"], "UNKNOWN")
        self.assertEqual(result["facts"]["max_userns"]["status"], "UNKNOWN")
        self.assertEqual(result["facts"]["runsc_file"]["status"], "UNKNOWN")
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

    def test_os_release_is_not_shell_and_rejects_injection(self):
        self.probe.files["os_release"] = 'ID="$(SYNTHETIC_SECRET)"\n'
        self.assertEqual(self.collect()["facts"]["os"]["status"], "UNKNOWN")

    def test_os_release_known_fallback_is_supported(self):
        self.probe.files["os_release_fallback"] = self.probe.files.pop("os_release")
        self.assertEqual(self.collect()["facts"]["os"]["ID"], "ubuntu")

    def test_subordinate_ranges_require_contiguous_allocation(self):
        for text in ("", "lab:100000:100\n", "lab:100000:32768\nlab:200000:32768\n", "lab:0:65536\n"):
            with self.subTest(text=text):
                self.probe.files["subuid"] = text
                self.assertEqual(self.collect()["facts"]["subuid"]["status"], "FAIL")

    def test_subordinate_overlap_across_owners_is_fail(self):
        self.probe.files["subgid"] = "lab:100000:65536\nother:120000:65536\n"
        self.assertEqual(self.collect()["facts"]["subgid"]["status"], "FAIL")

    def test_numeric_subordinate_owner_supported_without_name_exposure(self):
        self.probe.files["subuid"] = "1000:100000:65536\n"
        result = self.collect()["facts"]["subuid"]
        self.assertEqual(result["status"], "OBSERVED")
        self.assertNotIn("100000", json.dumps(result))
        self.assertNotIn("lab", json.dumps(result))

    def test_nonlocal_identity_does_not_query_nss(self):
        result = subordinate("lab:100000:65536", "other:x:2:2::/:/bin/false", 1000)
        self.assertEqual(result["status"], "UNKNOWN")

    def test_missing_controller_fails_and_presence_does_not_prove_delegation(self):
        self.probe.files["root_controllers"] = "cpu memory\n"
        result = self.collect()
        self.assertEqual(result["facts"]["root_controllers"]["status"], "FAIL")
        self.assertEqual(result["facts"]["current_controllers"]["delegation"], "UNKNOWN")

    def test_cgroup_v1_and_hybrid_are_not_unified_v2_pass(self):
        self.assertTrue(self.collect()["facts"]["cgroup_mount"]["unified_v2"])
        self.probe.files["mountinfo"] = "1 0 0:1 / /sys/fs/cgroup rw - tmpfs tmpfs rw\n"
        self.assertEqual(self.collect()["facts"]["cgroup_mount"]["status"], "FAIL")

    def test_mapping_helper_presence_is_not_effective_mapping_proof(self):
        self.assertEqual(self.collect()["facts"]["uid_mapping_helper"]["effective_mapping"], "NOT_RUN")
        with patch.object(self.probe, "mapping_helper", side_effect=PermissionError("SYNTHETIC_SECRET")):
            self.assertEqual(self.collect()["facts"]["gid_mapping_helper"]["status"], "UNKNOWN")

    def test_runtime_digest_and_architecture_mismatch_fail(self):
        self.probe.image["sha256"] = "2" * 64
        self.assertEqual(self.collect()["facts"]["runsc_file"]["status"], "FAIL")
        self.probe.image["architecture"] = "amd64"
        self.assertEqual(self.collect()["facts"]["runsc_file"]["reason"], "runtime_architecture_mismatch")

    def test_digest_match_does_not_invent_runtime_version(self):
        value = self.collect()["facts"]["runsc_file"]
        self.assertTrue(value["expected_digest_match"])
        self.assertEqual(value["version"], "UNKNOWN")
        self.assertEqual(value["systrap"], "NOT_RUN")

    def test_empty_disk_or_inodes_fail(self):
        for key in ("available_bytes", "available_inodes"):
            with self.subTest(key=key):
                self.probe.disk[key] = 0
                self.assertEqual(self.collect()["facts"]["storage"]["status"], "FAIL")

    def test_capacity_and_quota_flag_do_not_prove_enforcement(self):
        value = self.collect()["facts"]["storage"]
        self.assertTrue(value["quota_mount_option"])
        self.assertEqual(value["quota_enforcement"], "NOT_RUN")
        self.assertEqual(value["reservation_capacity"], "UNKNOWN")

    def test_default_cli_never_constructs_probe(self):
        output = io.StringIO()
        with patch("gvisor_host_preflight.check.LocalProbe", side_effect=AssertionError("not permitted")) as probe, redirect_stdout(output):
            self.assertEqual(main([]), 2)
        probe.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["collection"], "NOT_RUN")

    def test_cli_explicit_optin_is_still_blocked(self):
        output = io.StringIO()
        with patch("gvisor_host_preflight.check.LocalProbe", return_value=self.probe), redirect_stdout(output):
            self.assertEqual(main(["--inspect-dedicated-host", "--storage-path", "/synthetic/data"]), 2)
        self.assertEqual(json.loads(output.getvalue())["collection"], "COLLECTED")
        self.assertFalse(json.loads(output.getvalue())["launch_enabled"])

    def test_cli_never_echoes_unknown_argument_value(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(main(["--run", "SYNTHETIC_SECRET"]), 2)
        self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())

    def test_legacy_gate_and_recipe_hashes_remain_intact(self):
        result = evaluate()
        self.assertEqual(result["status"], "STATIC_CONSISTENT_ONLY")
        self.assertTrue(result["runtime_source_unchanged"])
        self.assertFalse(result["launch_enabled"])

    def test_documented_evidence_covers_all_checks_without_invented_results(self):
        root = Path(__file__).resolve().parents[2]
        template = json.loads((root / "docs/gvisor-host-preflight/evidence.example.json").read_text(encoding="utf-8"))
        self.assertEqual(set(template["checks"]), set(RUNTIME_CHECKS))
        self.assertTrue(all(value == "NOT_RUN" for value in template["checks"].values()))
        self.assertIsNone(template["runtime_release"])
        self.assertIsNone(template["runtime_bundle_digest"])
        self.assertIs(template["launch_enabled"], False)


class ProbeTests(unittest.TestCase):
    """一時ファイルとOS syscallモックで、実装が実行・通信・書込みしない境界を検査する。"""

    def test_paths_reject_traversal_relative_and_empty_components(self):
        for path in ("../secret", "/a/../b", "/a/./b", "/a//b", "/a/", "/a\x00b", "/"):
            with self.subTest(path=path), self.assertRaises(Unavailable):
                components(path)

    def test_open_uses_readonly_nofollow_and_closes_parent_fds(self):
        with patch("gvisor_host_preflight.probe.os.O_DIRECTORY", 0x100000, create=True), patch("gvisor_host_preflight.probe.os.O_NOFOLLOW", 0x200000, create=True), patch("gvisor_host_preflight.probe.os.O_NONBLOCK", 0x400000, create=True), patch("gvisor_host_preflight.probe.os.open", side_effect=[10, 11, 12]) as opened, patch("gvisor_host_preflight.probe.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)) as checked, patch("gvisor_host_preflight.probe.os.close") as closed:
            self.assertEqual(open_readonly("/a/file"), 12)
            for call in opened.call_args_list:
                flags = call.args[1]
                self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC), 0)
                self.assertTrue(flags & 0x200000)
            checked.assert_called_once_with("file", dir_fd=11, follow_symlinks=False)
            self.assertEqual([call.args[0] for call in closed.call_args_list], [10, 11])

    def test_leaf_symlink_or_special_file_is_not_opened(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR):
            with self.subTest(mode=mode), patch("gvisor_host_preflight.probe.os.O_DIRECTORY", 0x100000, create=True), patch("gvisor_host_preflight.probe.os.O_NOFOLLOW", 0x200000, create=True), patch("gvisor_host_preflight.probe.os.O_NONBLOCK", 0x400000, create=True), patch("gvisor_host_preflight.probe.os.open", side_effect=[10, 11]) as opened, patch("gvisor_host_preflight.probe.os.stat", return_value=SimpleNamespace(st_mode=mode)), patch("gvisor_host_preflight.probe.os.close"):
                with self.assertRaises(Unavailable):
                    open_readonly("/a/file")
                self.assertEqual(opened.call_count, 2)

    def test_runtime_on_remote_filesystem_is_not_opened(self):
        with patch.object(LocalProbe, "text", return_value="1 0 0:1 / / rw - autofs auto rw\n"), patch("gvisor_host_preflight.probe.open_readonly") as opened:
            with self.assertRaises(Unavailable):
                LocalProbe().binary("/synthetic/runsc")
            opened.assert_not_called()

    def test_overlapping_mount_visibility_is_unknown_not_guessed(self):
        mounts = "1 0 0:1 / / rw - ext4 /dev/lab rw\n2 0 0:2 / / rw - nfs server:/secret rw\n"
        with patch.object(LocalProbe, "text", return_value=mounts), patch("gvisor_host_preflight.probe.open_readonly") as opened:
            with self.assertRaises(Unavailable):
                LocalProbe().storage("/synthetic/data")
            opened.assert_not_called()

    def test_mountinfo_parsing_and_ambiguous_escape_rejection(self):
        records = mount_records("1 0 0:1 / / rw - ext4 /dev/lab rw,prjquota\n")
        self.assertEqual(records[0][2], "ext4")
        self.assertIn("prjquota", records[0][3])
        with self.assertRaises(Unavailable):
            mount_records("1 0 0:1 / /a\\040b rw - ext4 /dev/lab rw\n")

    def test_bounded_text_read_handles_proc_style_zero_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file"
            path.write_bytes(b"synthetic-data")
            def opened(*args, **kwargs):
                """Linux FD解決のみ代用する。通常ファイル読取りは実際に行う。"""
                return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            with patch("gvisor_host_preflight.probe.open_readonly", side_effect=opened):
                self.assertEqual(read_text("/synthetic/file"), "synthetic-data")
                with self.assertRaises(Unavailable):
                    read_text("/synthetic/file", maximum=4)
            self.assertEqual(path.read_bytes(), b"synthetic-data")

    def test_binary_header_and_digest_are_read_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runsc-candidate"
            data = bytearray(64)
            data[:6], data[18:20] = b"\x7fELF\x02\x01", (183).to_bytes(2, "little")
            path.write_bytes(data)
            real_fstat = os.fstat
            def fake_stat(fd):
                """Windowsで再現できないLinux実行bitだけを合成する。"""
                original = real_fstat(fd)
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_nlink=1, st_size=original.st_size,
                                       st_mtime_ns=original.st_mtime_ns, st_ctime_ns=original.st_ctime_ns)
            with patch.object(LocalProbe, "text", return_value="1 0 0:1 / / rw - ext4 /dev/lab rw\n"), patch("gvisor_host_preflight.probe.open_readonly", side_effect=lambda *a, **k: os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))), patch("gvisor_host_preflight.probe.os.fstat", side_effect=fake_stat):
                result = LocalProbe().binary("/synthetic/runsc")
            self.assertEqual(result["architecture"], "arm64")
            self.assertTrue(result["executable_bit"])
            self.assertEqual(len(result["sha256"]), 64)
            self.assertEqual(path.read_bytes(), bytes(data))

    def test_non_elf_binary_rejected(self):
        with patch.object(LocalProbe, "text", return_value="1 0 0:1 / / rw - ext4 /dev/lab rw\n"), patch("gvisor_host_preflight.probe.open_readonly", return_value=42), patch("gvisor_host_preflight.probe.os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_nlink=1, st_size=64)), patch("gvisor_host_preflight.probe.os.read", return_value=b"secret"), patch("gvisor_host_preflight.probe.os.close"):
            with self.assertRaises(Unavailable):
                LocalProbe().binary("/synthetic/runsc")

    def test_network_filesystem_is_rejected_before_open(self):
        probe = LocalProbe()
        with patch.object(probe, "text", return_value="1 0 0:1 / / rw - nfs server:/secret rw\n"), patch("gvisor_host_preflight.probe.open_readonly") as opened:
            with self.assertRaises(Unavailable):
                probe.storage("/synthetic/data")
            opened.assert_not_called()

    def test_current_cgroup_rejects_unsafe_membership_before_read(self):
        probe = LocalProbe()
        def read(path):
            """固定mountと不正なmembershipを返す。sysfsへの到達は失敗扱い。"""
            if path.endswith("mountinfo"):
                return "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
            if path.endswith("cgroup"):
                return "0::/../../secret\n"
            raise AssertionError("unsafe sysfs read")
        with patch("gvisor_host_preflight.probe.read_text", side_effect=read):
            with self.assertRaises(Unavailable):
                probe.text("current_cpu")

    def test_static_import_and_call_allowlist_excludes_process_and_network_clients(self):
        root = Path(__file__).resolve().parents[1] / "gvisor_host_preflight"
        allowed_imports = {"argparse", "json", "re", "hashlib", "os", "pathlib", "stat", "gvisor_host_preflight"}
        forbidden = {"popen", "spawn", "execv", "execve", "fork", "write", "mkdir", "unlink", "chmod", "chown", "truncate", "mount", "setns", "unshare", "socket", "connect"}
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(alias.name.split(".")[0] in allowed_imports for alias in node.names))
                if isinstance(node, ast.ImportFrom):
                    self.assertIn(node.module.split(".")[0], allowed_imports)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, forbidden)
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                        self.assertNotEqual(node.func.attr, "system")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"open", "eval", "exec", "__import__"})
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"})
