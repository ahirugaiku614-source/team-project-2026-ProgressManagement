"""設定生成の契約試験。Docker・Linuxへの接続や鍵の実配置は行わない。"""
import sys
from pathlib import Path
import unittest
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
from native_settings import generate
from app.workspace_settings import backend_settings


class NativeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.fixture={'fixture_id':str(uuid4()),'team_id':1,'project_id':1,'members':{
            'A':{'member_id':1,'task_id':1,'folder_path':'tasks/a'},'B':{'member_id':2,'task_id':2,'folder_path':'tasks/b'}}}
        self.ids={'a':1101,'b':1102,'manager':1201,'gateway':1202,'db':1301}
        self.bases={label:{'workspace_id':str(uuid4()),'source_generation':1,'project_id':1,
            'task_id':index,'member_id':index} for index,label in enumerate(('A','B'),1)}

    def test_phase_one_matches_actual_schema(self):
        outputs=generate(self.fixture,self.ids)
        backend_settings(outputs['backend.json']['workspace'])
        self.assertFalse(outputs['backend.json']['workspace']['gateway_enabled'])
        self.assertEqual(outputs['backend.json']['workspace']['collectors'],[])

    def test_phase_two_is_fixed_to_separate_members(self):
        outputs=generate(self.fixture,self.ids,self.bases)
        self.assertEqual(len(outputs['backend.json']['workspace']['collectors']),2)
        self.assertEqual(outputs['a-collector.json']['target']['student_id'],1)
        self.assertEqual(outputs['b-collector.json']['target']['student_id'],2)
        self.assertIn('/stage10d-native.html',outputs['nginx.conf'])

    def test_wrong_member_basis_refused(self):
        self.bases['A']['member_id']=2
        with self.assertRaises(ValueError):generate(self.fixture,self.ids,self.bases)

    def test_same_workspace_refused(self):
        from app.submission_provider import ProviderUnavailable
        self.bases['B']['workspace_id']=self.bases['A']['workspace_id']
        with self.assertRaises(ProviderUnavailable):generate(self.fixture,self.ids,self.bases)

    def test_same_os_identity_refused(self):
        self.ids['a']=self.ids['b']
        with self.assertRaises(ValueError):generate(self.fixture,self.ids)

    def test_path_escape_refused(self):
        self.fixture['members']['A']['folder_path']='../b'
        with self.assertRaises(Exception):generate(self.fixture,self.ids)

    def test_editor_cookie_name_is_supported_without_management_cookie_forwarding(self):
        from app.editor_socket_gateway import SocketGateway
        config=generate(self.fixture,self.ids,self.bases)['socket-gateway.json']
        SocketGateway(config['targets'],config['origin'],lambda *_:False,upstream_cookie_names=config['upstream_cookie_names'])
        with self.assertRaises(ValueError):SocketGateway(config['targets'],config['origin'],lambda *_:False,upstream_cookie_names=['work_editor_other'])


if __name__=='__main__':unittest.main()
