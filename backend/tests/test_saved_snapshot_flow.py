"""方式Cを実DB認可・署名RPC・Git保存・既存ChangeRequestへ接続する合成試験。"""
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_work_environment as fixtures
from app import models
from app.work_environment import SnapshotEditingSessions
from app.task_review_api import get_review_sessions
from app.submission_provider import get_submission_provider
from app.submission_authorization import authorized_rpc_provider, CollectorAuthorizationVerifier
from app.submission_rpc import RPCKeys, CollectorRPCServer, MemoryReplayLedger
from app.submission_git import GitSnapshotStore


class SavedSnapshotTests(unittest.TestCase):
    setUpClass=fixtures.EnvironmentTests.__dict__['setUpClass']
    tearDownClass=fixtures.EnvironmentTests.__dict__['tearDownClass']
    tearDown=fixtures.EnvironmentTests.tearDown
    headers=staticmethod(fixtures.EnvironmentTests.headers)
    decode_token=staticmethod(fixtures.EnvironmentTests.decode_token)
    register=fixtures.EnvironmentTests.register
    capture=fixtures.EnvironmentTests.capture
    start=fixtures.EnvironmentTests.start
    request=fixtures.EnvironmentTests.request
    prepared=fixtures.EnvironmentTests.prepared
    preview=fixtures.EnvironmentTests.preview
    open_session=fixtures.EnvironmentTests.open_session

    def setUp(self):
        fixtures.EnvironmentTests.setUp(self)
        self.sessions=SnapshotEditingSessions(self.db)
        self.runtime.sessions=self.sessions
        self.main.app.dependency_overrides[get_review_sessions]=lambda:self.sessions
        self.calls=[]
        self.saved_files={'main.txt':('100644',b'synthetic saved content\n')}

    def collect_saved(self):
        from app.submission_scope import SubmissionTarget
        b=self.start();sid=self.open_session(b)
        self.workspaces.sessions[sid]=(b.id,b.workspace_id,b.source_generation)
        self.sid=sid
        target=SubmissionTarget(b.team_id,b.project_id,b.task_id,b.member_id,b.folder_path)
        keys=RPCKeys(b'synthetic-snapshot-request-key'*2,b'synthetic-snapshot-response-key'*2)
        auth=b'synthetic-snapshot-authorization-key'*2
        def collect(target,session):
            self.calls.append(session)
            return dict(self.saved_files)
        self.verifier=CollectorAuthorizationVerifier(auth,'snapshot-a',target,1,workspace_id=b.workspace_id)
        self.server=CollectorRPCServer(keys,MemoryReplayLedger(),{target:(1,collect)},self.verifier)
        outer=self
        class Transport:
            def exchange(self,frame,**kwargs):return outer.server.handle(frame)
        self.provider=authorized_rpc_provider(self.db,self.member,rpc_keys=keys,authorization_key=auth,
            collector_id='snapshot-a',target=target,generation=1,store=GitSnapshotStore(self.git.session),
            transport=Transport(),sessions=self.sessions,ledger=MemoryReplayLedger())
        self.main.app.dependency_overrides[get_submission_provider]=lambda:self.provider
        base=f'/api/teams/{b.team_id}/projects/{b.project_id}/tasks/{b.task_id}'
        permit=self.client.post(base+'/submission-permits',json={'editing_session_id':sid},headers=self.headers('member')).json()['permit']
        self.collection_body={'request_id':str(uuid4()),'editing_session_id':sid,'permit':permit}
        response=self.request('POST','/'+b.id+'/collections',self.collection_body)
        self.assertEqual(response.status_code,202)
        self.assertEqual(response.json()['collection_state'],'SUBMITTED')
        self.basis=b
        return b,{'submission_id':response.json()['submission_id'],'title':'Synthetic saved snapshot'}

    def confirmed(self):
        b,payload=self.collect_saved()
        view=self.preview(b,payload)
        return b,{**payload,'confirmation_id':view['confirmation_id']},view

    def test_saved_snapshot_has_no_dirty_claim(self):
        b,p=self.collect_saved()
        evidence=self.sessions.verify(self.provider_target(b),self.sid,1)
        self.assertFalse(hasattr(evidence,'saved'))
        self.assertEqual(evidence.workspace_id,b.workspace_id)
        self.assertEqual(self.calls,[self.sid])

    def provider_target(self,b):
        from app.submission_scope import SubmissionTarget
        return SubmissionTarget(b.team_id,b.project_id,b.task_id,b.member_id,b.folder_path)

    def test_changes_after_confirmation_do_not_change_submitted_tree(self):
        b,p,v=self.confirmed()
        self.saved_files['main.txt']=('100644',b'changed after confirmation')
        self.saved_files['new.txt']=('100644',b'not confirmed')
        with patch.object(self.provider,'collect',side_effect=AssertionError('must not recollect')):
            response=self.request('POST','/'+b.id+'/change-request',p)
        self.assertEqual(response.status_code,200)
        row=self.db.get(models.ChangeRequest,response.json()['change_request_id'])
        from app.task_review import checked_files
        files=checked_files(self.git,row.snapshot_commit)
        self.assertEqual(files[b.folder_path+'/main.txt'][1],b'synthetic saved content\n')
        self.assertNotIn(b.folder_path+'/new.txt',files)
        self.assertEqual(self.calls,[self.sid])

    def test_preview_and_save_never_create_review_request(self):
        b,p,v=self.confirmed()
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)
        self.saved_files['main.txt']=('100644',b'more saved edits')
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)
        self.assertEqual(self.git.read_ref(('heads','main')),self.base)

    def test_unrelated_unsaved_buffer_is_never_read(self):
        # 未保存本文を渡す入口自体を拒否する。単に未使用のBufferを置く試験に
        # せず、実ルーターのextra=forbidと収集回数を接続して確認する。
        b,p,v=self.confirmed()
        response=self.request('POST','/'+b.id+'/collections',{
            **self.collection_body,'request_id':str(uuid4()),'buffer_text':'synthetic unsaved draft'})
        self.assertEqual(response.status_code,422)
        self.assertNotIn('synthetic unsaved draft',response.text)
        self.assertEqual(self.request('POST','/'+b.id+'/change-request',p).status_code,200)
        self.assertEqual(self.calls,[self.sid])

    def test_same_confirmation_is_idempotent_but_changed_title_is_rejected(self):
        b,p,_=self.confirmed()
        a=self.request('POST','/'+b.id+'/change-request',p)
        again=self.request('POST','/'+b.id+'/change-request',p)
        self.assertEqual(a.status_code,200);self.assertEqual(again.status_code,200)
        self.assertEqual(a.json()['change_request_id'],again.json()['change_request_id'])
        self.assertEqual(self.db.query(models.ChangeRequest).count(),1)
        self.assertEqual(self.request('POST','/'+b.id+'/change-request',{**p,'title':'different'}).status_code,409)

    def test_other_member_cannot_use_confirmation(self):
        b,p,_=self.confirmed()
        self.assertEqual(self.request('POST','/'+b.id+'/change-request',p,uid='owner').status_code,403)

    def test_revocation_blocks_conversion_without_changing_snapshot(self):
        b,p,_=self.confirmed()
        row=self.db.get(models.TaskSubmission,p['submission_id']);before=row.commit_id
        self.db.get(models.WorkEditingSession,self.sid).revoked_at=datetime.utcnow();self.db.commit()
        self.assertEqual(self.request('POST','/'+b.id+'/change-request',p).status_code,503)
        self.assertEqual(self.db.get(models.TaskSubmission,row.id).commit_id,before)
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_expired_confirmation_is_rejected(self):
        b,p,_=self.confirmed()
        self.db.get(models.WorkReviewPreview,p['confirmation_id']).expires_at=datetime.utcnow()-timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(self.request('POST','/'+b.id+'/change-request',p).status_code,409)

    def test_request_lookup_after_response_loss_never_recollects(self):
        b,p,_=self.confirmed()
        self.request('POST','/'+b.id+'/change-request',p)
        with patch.object(self.provider,'collect',side_effect=AssertionError('must not recollect')):
            response=self.request('GET','/'+b.id+'/requests/'+self.collection_body['request_id'])
        self.assertEqual(response.status_code,200)
        self.assertIsNotNone(response.json()['change_request_id'])
        self.assertEqual(self.calls,[self.sid])

    def test_foreign_workspace_rejected_before_collection(self):
        b,p=self.collect_saved();before=len(self.calls)
        self.verifier.workspace_id=str(uuid4())
        # 再収集ではなく別の明示操作。固定workspaceが異なる署名は通さない。
        base=f'/api/teams/{b.team_id}/projects/{b.project_id}/tasks/{b.task_id}'
        permit=self.client.post(base+'/submission-permits',json={'editing_session_id':self.sid},headers=self.headers('member')).json()['permit']
        response=self.request('POST','/'+b.id+'/collections',{'request_id':str(uuid4()),'editing_session_id':self.sid,'permit':permit})
        self.assertEqual(response.json()['collection_state'],'REVIEW_REQUIRED')
        self.assertEqual(len(self.calls),before)

    def test_expired_session_rejects_preview(self):
        b,p=self.collect_saved()
        self.db.get(models.WorkEditingSession,self.sid).expires_at=datetime.utcnow()-timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(self.request('POST','/'+b.id+'/preview',p).status_code,503)

    def test_session_cannot_be_used_for_another_basis(self):
        b,p=self.collect_saved();other=self.start()
        base=f'/api/teams/{b.team_id}/projects/{b.project_id}/tasks/{b.task_id}'
        permit=self.client.post(base+'/submission-permits',json={'editing_session_id':self.sid},headers=self.headers('member')).json()['permit']
        response=self.request('POST','/'+other.id+'/collections',{'request_id':str(uuid4()),'editing_session_id':self.sid,'permit':permit})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.calls,[self.sid])

    def test_forged_confirmation_is_rejected(self):
        b,p,_=self.confirmed()
        self.assertEqual(self.request('POST','/'+b.id+'/change-request',{**p,'confirmation_id':str(uuid4())}).status_code,409)

    def test_latest_generation_keeps_old_basis_and_session(self):
        b,p=self.collect_saved()
        response=self.request('POST','/environment',{'request_id':str(uuid4()),'previous_basis_id':b.id,'session_id':self.sid})
        self.assertEqual(response.status_code,200)
        self.assertNotEqual(response.json()['basis_id'],b.id)
        self.assertEqual(self.db.get(models.WorkBasis,b.id,populate_existing=True).state,'ACTIVE')
        self.assertIsNone(self.db.get(models.WorkEditingSession,self.sid).revoked_at)

    def test_latest_refuses_saved_unsubmitted_changes(self):
        b,p=self.collect_saved();self.workspaces.unchanged=lambda basis:False
        response=self.request('POST','/environment',{'request_id':str(uuid4()),'previous_basis_id':b.id,'session_id':self.sid})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.db.get(models.WorkBasis,b.id).state,'ACTIVE')

    def test_explicit_runtime_is_disabled_without_approval(self):
        from app.snapshot_runtime import configured_snapshot_provider,configured_snapshot_collector
        from app.submission_provider import ProviderUnavailable
        with self.assertRaises(ProviderUnavailable):configured_snapshot_provider(None,None,None,None,{})
        with self.assertRaises(ProviderUnavailable):configured_snapshot_collector({})

    def test_bootstrap_gateway_uses_bound_cookie_without_firebase_team_query(self):
        from app.workspace_bootstrap import install_backend
        b,p=self.collect_saved()
        access=self.request('POST','/'+b.id+'/editor-access',{'session_id':self.sid})
        self.assertEqual(access.status_code,200)
        token=access.cookies.get('work_editor_'+b.workspace_id)
        self.assertIsNotNone(token)
        # gateは本人を既存capabilityから特定する。任意UserやTeamを送らない。
        install_backend(self.main.app,self.main.get_project_team_member,[],enabled=True,gateway_enabled=True)
        response=self.client.get('/api/editor-gateway/'+b.workspace_id,
            headers={'Cookie':'work_editor_'+b.workspace_id+'='+token})
        self.assertEqual(response.status_code,204)

    def test_saved_snapshot_reuses_real_review_approval_and_integration(self):
        from app.project_storage import get_project_storage
        from app.review_git import ReviewGit
        import app.change_request_integration as integration
        import stage9_git_adapter as portable
        b,p,_=self.confirmed()
        response=self.request('POST','/'+b.id+'/change-request',p)
        self.assertEqual(response.status_code,200)
        cr=response.json()['change_request_id']
        self.main.app.dependency_overrides[get_project_storage]=lambda:object()
        self.git.project=Path(self.temp.name)
        path=f'/api/teams/{b.team_id}/projects/{b.project_id}/change-requests/{cr}'
        with patch.object(ReviewGit,'session',side_effect=self.git.session), \
             patch.object(self.git,'new_directory',side_effect=portable.directory), \
             patch.object(integration,'IntegrationJournal',portable.PortableJournal), \
             patch.object(integration,'locked_main',portable.locked_main), \
             patch.object(integration,'sync_main',portable.sync_main):
            self.assertEqual(self.client.get(path+'/diff',headers=self.headers('owner')).status_code,200)
            self.assertEqual(self.client.post(path+'/approve',json={},headers=self.headers('member')).status_code,403)
            self.assertEqual(self.client.post(path+'/approve',json={},headers=self.headers('owner')).status_code,200)
            self.assertEqual(self.client.post(path+'/integrate',json={},headers=self.headers('owner')).status_code,200)
        self.assertNotEqual(self.git.read_ref(('heads','main')),self.base)
        self.assertEqual(self.calls,[self.sid])
