"""Stage 9の合成API・セッション・新世代契約。実エディターの真正性は証明しない。"""
from dataclasses import replace
from datetime import datetime, timedelta
import sys
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_task_review_api as fixtures
from app import models
from app.task_review_api import get_work_environment, get_review_sessions
from app.work_environment import EditingSessions, EditorAttestation, WorkEnvironment
from app.submission_provider import ProviderUnavailable
from app.submission_scope import SubmissionTarget


class SyntheticAuthority:
    """管理側fixtureだけから状態を設定する。HTTPや拡張の自己申告とは接続しない。"""
    def __init__(self):self.saved=True;self.exclusive=True;self.replay=False
    def inspect(self, sid, basis, challenge):
        now = int(time.time())
        return EditorAttestation(sid, basis.id, basis.workspace_id, basis.source_generation,
            'old-challenge' if self.replay else challenge, now, now+20, self.saved, self.exclusive)


class EnvironmentTests(unittest.TestCase):
    setUpClass=fixtures.ReviewAPITests.__dict__['setUpClass']
    tearDownClass=fixtures.ReviewAPITests.__dict__['tearDownClass']
    tearDown=fixtures.ReviewAPITests.tearDown
    headers=staticmethod(fixtures.ReviewAPITests.headers)
    decode_token=staticmethod(fixtures.ReviewAPITests.decode_token)
    register=fixtures.ReviewAPITests.register
    capture=fixtures.ReviewAPITests.capture
    start=fixtures.ReviewAPITests.start
    request=fixtures.ReviewAPITests.request
    prepared=fixtures.ReviewAPITests.prepared
    preview=fixtures.ReviewAPITests.preview

    def setUp(self):
        fixtures.ReviewAPITests.setUp(self)
        self.authority=SyntheticAuthority()
        self.trusted_sessions=EditingSessions(self.db,self.authority)
        self.runtime=WorkEnvironment(self.service,self.trusted_sessions,editor_gateway=True)
        self.workspaces.unchanged=lambda basis:True
        self.main.app.dependency_overrides[get_work_environment]=lambda:self.runtime

    def open_session(self,basis):return self.request('POST','/'+basis.id+'/editing-sessions',{}).json()['session_id']

    def test_version_and_workspace_are_server_bound(self):
        b=self.start();r=self.request('GET','/'+b.id).json()
        self.assertEqual(r['base_version'],self.base);self.assertEqual(r['workspace_id'],b.workspace_id)

    def test_lost_collection_response_lookup_is_read_only(self):
        b,s,_=self.prepared();before=self.git.read_ref(('heads','main'));count=self.db.query(models.TaskSubmission).count()
        with patch.object(self.git,'journal_create',side_effect=AssertionError('GET must not write')):
            r=self.request('GET','/'+b.id+'/requests/'+s.request_id)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['submission_id'],s.id)
        self.assertEqual(self.db.query(models.TaskSubmission).count(),count);self.assertEqual(self.git.read_ref(('heads','main')),before)

    def test_missing_request_is_unknown_not_not_executed(self):
        b=self.start();r=self.request('GET','/'+b.id+'/requests/'+str(uuid4())).json()
        self.assertEqual(r['outcome'],'UNKNOWN');self.assertIsNone(r['submission_id'])

    def test_other_member_cannot_lookup_request(self):
        b,s,_=self.prepared();self.assertEqual(self.request('GET','/'+b.id+'/requests/'+s.request_id,uid='owner').status_code,403)

    def test_request_cannot_cross_basis(self):
        b,s,_=self.prepared();other=self.start()
        self.assertEqual(self.request('GET','/'+other.id+'/requests/'+s.request_id).status_code,409)

    def test_confirmation_status_and_expiry_do_not_convert(self):
        b,s,p=self.prepared();v=self.preview(b,p);path='/'+b.id+'/confirmations/'+v['confirmation_id']
        self.assertEqual(self.request('GET',path).json()['state'],'AVAILABLE')
        self.db.get(models.WorkReviewPreview,v['confirmation_id']).expires_at=datetime.utcnow()-timedelta(seconds=1);self.db.commit()
        self.assertEqual(self.request('GET',path).json()['state'],'EXPIRED');self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_confirmation_cannot_cross_basis(self):
        b,s,p=self.prepared();v=self.preview(b,p);other=self.start()
        self.assertEqual(self.request('GET','/'+other.id+'/confirmations/'+v['confirmation_id']).status_code,404)

    def test_deletion_evidence_is_scoped_and_contains_no_blob(self):
        b,s,p=self.prepared();r=self.request('GET','/'+b.id+'/deletion-intents?submission_id='+s.id)
        self.assertEqual(r.json(),{'intents':[]})

    def test_session_is_bound_and_revocable(self):
        b=self.start();sid=self.open_session(b);target=SubmissionTarget(b.team_id,b.project_id,b.task_id,b.member_id,b.folder_path)
        self.assertTrue(self.trusted_sessions.verify(target,sid,b.source_generation).saved)
        self.db.get(models.WorkEditingSession,sid).revoked_at=datetime.utcnow();self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.trusted_sessions.verify(target,sid,b.source_generation)

    def test_session_replay_and_multiple_editors_are_rejected(self):
        b=self.start();sid=self.open_session(b);target=SubmissionTarget(b.team_id,b.project_id,b.task_id,b.member_id,b.folder_path)
        self.authority.replay=True
        with self.assertRaises(ProviderUnavailable):self.trusted_sessions.verify(target,sid,1)
        self.authority.replay=False;self.authority.exclusive=False
        with self.assertRaises(ProviderUnavailable):self.trusted_sessions.verify(target,sid,1)

    def test_default_authority_does_not_trust_session_id(self):
        b=self.start();sid=self.open_session(b);target=SubmissionTarget(b.team_id,b.project_id,b.task_id,b.member_id,b.folder_path)
        with self.assertRaises(ProviderUnavailable):EditingSessions(self.db).verify(target,sid,1)

    def test_environment_prepare_is_idempotent_and_keeps_main(self):
        key=str(uuid4());a=self.request('POST','/environment',{'request_id':key}).json()
        b=self.request('POST','/environment',{'request_id':key}).json()
        self.assertEqual(a,b);self.assertEqual(a['state'],'CONFIRMED');self.assertEqual(self.db.query(models.WorkBasis).count(),1)
        self.assertEqual(self.git.read_ref(('heads','main')),self.base)

    def test_environment_request_refuses_different_input(self):
        key=str(uuid4());self.request('POST','/environment',{'request_id':key})
        r=self.request('POST','/environment',{'request_id':key,'previous_basis_id':str(uuid4())})
        self.assertEqual(r.status_code,409)

    def test_latest_refuses_dirty_and_saved_unsubmitted_changes(self):
        b=self.start();sid=self.open_session(b);p={'request_id':str(uuid4()),'previous_basis_id':b.id,'session_id':sid}
        self.authority.saved=False
        self.assertEqual(self.request('POST','/environment',p).json()['detail']['code'],'UNSAVED_CHANGES')
        self.authority.saved=True;self.workspaces.unchanged=lambda basis:False
        self.assertEqual(self.request('POST','/environment',p).json()['detail']['code'],'UNSUBMITTED_CHANGES')
        self.assertEqual(self.db.query(models.WorkBasis).count(),1)

    def test_latest_uses_new_workspace_and_retains_old(self):
        b=self.start();sid=self.open_session(b);old_workspace=b.workspace_id
        r=self.request('POST','/environment',{'request_id':str(uuid4()),'previous_basis_id':b.id,'session_id':sid})
        self.assertEqual(r.status_code,200);new=self.db.get(models.WorkBasis,r.json()['basis_id'])
        self.assertNotEqual(new.workspace_id,old_workspace);self.assertEqual(self.db.get(models.WorkBasis,b.id).state,'SUPERSEDED')
        self.assertEqual(self.git.basis_ref(b.id),self.base)

    def test_unknown_prepare_retains_admission_and_does_not_retry(self):
        key=str(uuid4())
        with patch.object(self.service,'start',side_effect=RuntimeError('synthetic preparation failure')) as initialize:
            self.assertEqual(self.request('POST','/environment',{'request_id':key}).status_code,503)
            r=self.request('POST','/environment',{'request_id':key})
            self.assertEqual(r.json()['state'],'PROCESSING');self.assertEqual(initialize.call_count,1)

    def test_access_cookie_is_short_lived_scoped_and_not_in_json(self):
        b=self.start();sid=self.open_session(b)
        r=self.request('POST','/'+b.id+'/editor-access',{'session_id':sid})
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['url'],'/code-server/'+b.workspace_id+'/')
        cookie=r.headers['set-cookie'];self.assertIn('Secure',cookie);self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=strict',cookie)
        name='work_editor_'+b.workspace_id;token=r.cookies[name]
        self.assertNotIn(token,r.text);self.assertNotEqual(self.db.query(models.WorkEditorAccess).one().token_digest,token)
        headers={'Cookie':name+'='+token}
        self.assertEqual(self.client.get('/api/editor-gateway/'+b.workspace_id,headers=headers).status_code,204)
        self.assertEqual(self.client.get('/api/editor-gateway/'+str(uuid4()),headers=headers).status_code,401)

    def test_access_revocation_is_enforced_on_each_read(self):
        b=self.start();sid=self.open_session(b);r=self.request('POST','/'+b.id+'/editor-access',{'session_id':sid})
        name='work_editor_'+b.workspace_id;headers={'Cookie':name+'='+r.cookies[name]}
        self.db.get(models.WorkEditingSession,sid).revoked_at=datetime.utcnow();self.db.commit()
        self.assertEqual(self.client.get('/api/editor-gateway/'+b.workspace_id,headers=headers).status_code,503)

    def test_default_runtime_rejects_prepare_and_access(self):
        self.main.app.dependency_overrides[get_work_environment]=lambda:WorkEnvironment(self.service)
        self.assertFalse(self.request('GET','/environment').json()['prepare_enabled'])
        self.assertEqual(self.request('POST','/environment',{'request_id':str(uuid4())}).status_code,503)

    def test_unknown_environment_read_does_not_initialize(self):
        r=self.request('GET','/environment/requests/'+str(uuid4())).json()
        self.assertEqual(r['state'],'UNKNOWN');self.assertEqual(self.db.query(models.WorkBasis).count(),0)

    def test_old_start_cannot_bypass_durable_environment_admission(self):
        with patch.object(self.workspaces,'initialize_from_snapshot',create=True) as initialize:
            r=self.request('POST',payload={})
        self.assertEqual(r.status_code,409);self.assertEqual(r.json()['detail']['code'],'ENVIRONMENT_REQUEST_REQUIRED')
        initialize.assert_not_called();self.assertEqual(self.db.query(models.WorkBasis).count(),0)

    def test_review_context_reuses_existing_change_request(self):
        b,s,p=self.prepared();v=self.preview(b,p);p['confirmation_id']=v['confirmation_id']
        result=self.request('POST','/'+b.id+'/change-request',p).json()
        url=f'/api/teams/{self.team.id}/projects/{self.project.id}/change-requests/{result["change_request_id"]}/task-context'
        r=self.client.get(url,headers=self.headers('owner'))
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['task_id'],self.task.id)

    def test_expired_session_and_other_task_rejected(self):
        b=self.start();sid=self.open_session(b);target=SubmissionTarget(b.team_id,b.project_id,b.task_id,b.member_id,b.folder_path)
        with self.assertRaises(ProviderUnavailable):self.trusted_sessions.verify(replace(target,task_id=999),sid,1)
        self.db.get(models.WorkEditingSession,sid).expires_at=datetime.utcnow()-timedelta(seconds=1);self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.trusted_sessions.verify(target,sid,1)

    def test_review_registration_ack_loss_preserves_confirmed_request(self):
        b,s,p=self.prepared();v=self.preview(b,p);original=self.db.commit;lost=[]
        def commit():
            original()
            converted=self.db.get(models.TaskReviewConversion,s.id)
            if converted is not None and converted.state=='READY' and not lost:
                lost.append(True);raise RuntimeError('synthetic commit acknowledgement loss')
        with patch.object(self.db,'commit',side_effect=commit):
            converted=self.service.convert(self.member,b.id,s.id,p['title'],confirmation_id=v['confirmation_id'])
        self.assertEqual(converted.state,'READY');self.assertEqual(self.db.query(models.ChangeRequest).count(),1)
        self.assertEqual(self.git.read_ref(('heads','main')),self.base)

    def test_two_members_collect_review_integrate_and_prepare_latest(self):
        # 認可・HMAC・DB・Git変換・レビュー・統合は製品処理。OS境界だけを
        # portable adapterへ置き換え、実Linuxで成功したとは扱わない。
        from pathlib import Path
        from app.submission_authorization import authorized_rpc_provider, CollectorAuthorizationVerifier
        from app.submission_rpc import RPCKeys, CollectorRPCServer, MemoryReplayLedger
        from app.submission_git import GitSnapshotStore
        from app.submission_provider import get_submission_provider
        from app.project_storage import get_project_storage
        from app.review_git import ReviewGit
        import app.change_request_integration as integration
        import stage9_git_adapter as portable
        b=self.start();sid=self.open_session(b)
        self.workspaces.sessions[sid]=(b.id,b.workspace_id,b.source_generation)
        self.main.app.dependency_overrides[get_review_sessions]=lambda:self.trusted_sessions
        target=SubmissionTarget(b.team_id,b.project_id,b.task_id,b.member_id,b.folder_path)
        keys=RPCKeys(b'synthetic-demo-request-key'*2,b'synthetic-demo-response-key'*2)
        key=b'synthetic-demo-authorization-key'*2;calls=[]
        def collect(target,session):calls.append(1);return {'main.txt':('100644',b'synthetic edited file\n')}
        verifier=CollectorAuthorizationVerifier(key,'collector-a',target,1,sessions=self.trusted_sessions)
        rpc=CollectorRPCServer(keys,MemoryReplayLedger(),{target:(1,collect)},verifier)
        class Transport:
            def exchange(self,frame,**kwargs):return rpc.handle(frame)
        provider=authorized_rpc_provider(self.db,self.member,rpc_keys=keys,authorization_key=key,
            collector_id='collector-a',target=target,generation=1,store=GitSnapshotStore(self.git.session),
            transport=Transport(),sessions=self.trusted_sessions,ledger=MemoryReplayLedger())
        self.main.app.dependency_overrides[get_submission_provider]=lambda:provider
        base=f'/api/teams/{self.team.id}/projects/{self.project.id}/tasks/{self.task.id}'
        permit=self.client.post(base+'/submission-permits',json={'editing_session_id':sid},headers=self.headers('member')).json()['permit']
        captured=self.request('POST','/'+b.id+'/collections',{'request_id':str(uuid4()),
            'editing_session_id':sid,'permit':permit,'has_unsaved_changes':False})
        self.assertEqual(captured.status_code,202)
        payload={'submission_id':captured.json()['submission_id'],'title':'合成メンバーAの変更'}
        view=self.preview(b,payload);payload['confirmation_id']=view['confirmation_id']
        submitted=self.request('POST','/'+b.id+'/change-request',payload)
        self.assertEqual(submitted.status_code,200);cr=submitted.json()['change_request_id']
        self.main.app.dependency_overrides[get_project_storage]=lambda:object()
        self.git.project=Path(self.temp.name)
        path=f'/api/teams/{self.team.id}/projects/{self.project.id}/change-requests/{cr}'
        with patch.object(ReviewGit,'session',side_effect=self.git.session), \
             patch.object(self.git,'new_directory',side_effect=portable.directory), \
             patch.object(integration,'IntegrationJournal',portable.PortableJournal), \
             patch.object(integration,'locked_main',portable.locked_main), \
             patch.object(integration,'sync_main',portable.sync_main):
            self.assertEqual(self.client.post(path+'/approve',json={},headers=self.headers('member')).status_code,403)
            self.assertEqual(self.client.get(path+'/diff',headers=self.headers('owner')).status_code,200)
            self.assertEqual(self.client.post(path+'/approve',json={},headers=self.headers('owner')).status_code,200)
            integrated=self.client.post(path+'/integrate',json={},headers=self.headers('owner'))
            self.assertEqual(integrated.status_code,200)
        self.assertEqual(integrated.json()['status'],'COMPLETED');self.assertEqual(calls,[1])
        new_main=self.git.read_ref(('heads','main'));self.assertNotEqual(new_main,self.base)
        # Bの別タスクを登録し、共有コードの新しい基準から専用環境を準備する。
        from app.submission_targets import register_submission_target
        task=models.Task(title='合成メンバーBのタスク',user_id=self.owner.id,team_id=self.team.id,assignee_id=self.owner.id)
        self.db.add(task);self.db.commit()
        register_submission_target(self.db,self.owner,team_id=self.team.id,project_id=self.project.id,
            task_id=task.id,student_id=self.owner.id,folder_path='member-b')
        self.db.commit()
        other=self.service.start(self.owner,self.team.id,self.project.id,task.id)
        self.assertEqual(other.base_commit,new_main);self.assertNotEqual(other.workspace_id,b.workspace_id)
        self.assertEqual(self.git.basis_ref(b.id),self.base)


class WorkspacePlatformTests(unittest.TestCase):
    def test_windows_does_not_enable_linux_initializer(self):
        from app.workspace_files import LinuxWorkspaces
        if sys.platform=='linux':self.skipTest('Windows default-deny contract')
        with self.assertRaises(ProviderUnavailable):LinuxWorkspaces('/not-used',1001,1001,member_id=1,project_id=2)


class PreviewDiffTests(unittest.TestCase):
    def test_confirmation_diff_does_not_execute_html_or_code(self):
        from app.task_review import preview_diff
        result=preview_diff({'task/a':('100644',b'old\n')},{'task/a':('100644',b'<script>synthetic</script>\n')},[{'path':'task/a','change':'MODIFIED'}])
        self.assertIn('+<script>synthetic</script>',result['files'][0]['patch'])
        self.assertFalse(result['truncated'])

    def test_binary_confirmation_does_not_render_body(self):
        from app.task_review import preview_diff
        result=preview_diff({}, {'task/a':('100644',b'\0synthetic')},[{'path':'task/a','change':'ADDED'}])
        self.assertTrue(result['files'][0]['binary']);self.assertIsNone(result['files'][0]['patch'])

    def test_confirmation_diff_has_total_size_limit(self):
        from app.task_review import preview_diff
        from app.review_git import MAX_PATCH_BYTES
        result=preview_diff({}, {'task/a':('100644',b'x'*(MAX_PATCH_BYTES+10))},[{'path':'task/a','change':'ADDED'}])
        self.assertTrue(result['truncated']);self.assertLessEqual(len(result['files'][0]['patch'].encode()),MAX_PATCH_BYTES)
