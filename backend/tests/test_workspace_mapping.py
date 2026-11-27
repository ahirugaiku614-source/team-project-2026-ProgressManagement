"""固定対応表の純粋契約。Linuxの実ファイルへ接続しない。"""
from types import SimpleNamespace
import unittest
from app.workspace_files import MappedWorkspaces
from app.submission_provider import ProviderUnavailable


class WorkspaceMappingTests(unittest.TestCase):
    def test_other_member_and_project_are_not_routed(self):
        adapter=SimpleNamespace(member_id=1,project_id=2)
        mapping=MappedWorkspaces({(1,2):adapter})
        self.assertIs(mapping.adapter(1,2),adapter)
        for key in [(2,2),(1,3)]:
            with self.assertRaises(ProviderUnavailable):mapping.adapter(*key)

    def test_misconfigured_mapping_is_rejected(self):
        with self.assertRaises(ProviderUnavailable):MappedWorkspaces({(1,2):SimpleNamespace(member_id=2,project_id=2)})

    def test_mapping_cannot_be_mutated_after_configuration(self):
        source={(1,2):SimpleNamespace(member_id=1,project_id=2)}
        mapping=MappedWorkspaces(source);source.clear()
        self.assertEqual(len(mapping.adapters),1)
        with self.assertRaises(TypeError):mapping.adapters[(2,2)]=object()
        from app.workspace_files import LinuxWorkspaces
        self.assertEqual(LinuxWorkspaces.entry_mode('100644'),0o600)
        self.assertEqual(LinuxWorkspaces.entry_mode('100755'),0o700)
        self.assertEqual(LinuxWorkspaces.entry_mode('100644') & 0o022,0)
