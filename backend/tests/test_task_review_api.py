"""Windows上の合成Git/SQLite/API契約。実セッション・Linux排他性の実証ではない。"""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text
from app import models
from app.task_review import TaskReviewService, merge_task, TaskReviewError
from app.task_review_lifecycle import advance_workspace, require_lifecycle, trigger_sql, install_synthetic_lifecycle
from app.task_review_api import get_task_review_service, get_review_sessions
from app.submission_authorization import VerifiedSession, DeniedSessionVerifier
from app.submission_provider import get_submission_provider, DeniedSubmissionProvider
from app.project_storage import StorageConflict, StorageUnavailable
from app.project_storage import get_project_storage
import test_task_review as internal
import test_submissions as fixtures


class FakeSessions:
    """合成workspaceの検証済みsession登録だけを参照し、HTTP入力を証拠にしない。"""
    def __init__(self, workspaces):self.workspaces=workspaces;self.saved=True;self.valid=True
    def verify(self, target, session, generation):
        if not self.valid or session not in self.workspaces.sessions:raise ValueError('Unverified')
        return VerifiedSession(target,session,generation,int(time.time())+60,self.saved)


class LifecycleTests(unittest.TestCase):
    setUpClass=fixtures.SubmissionAPITests.__dict__['setUpClass']
    tearDownClass=fixtures.SubmissionAPITests.__dict__['tearDownClass']
    tearDown=fixtures.SubmissionAPITests.tearDown
    headers=staticmethod(fixtures.SubmissionAPITests.headers)
    decode_token=staticmethod(fixtures.SubmissionAPITests.decode_token)
    register=fixtures.SubmissionAPITests.register

    def setUp(self):
        fixtures.SubmissionAPITests.setUp(self)
        with self.engine.begin() as connection:install_synthetic_lifecycle(connection)
        self.basis=models.WorkBasis(id=str(uuid4()),member_id=self.member.id,team_id=self.team.id,
            project_id=self.project.id,task_id=self.task.id,workspace_id=str(uuid4()),source_generation=1,
            folder_path='assignments/task-1',binding_version='a'*64,base_commit='b'*40,
            policy_version=1,created_at=datetime.utcnow(),state='ACTIVE')
        self.db.add(self.basis)
        self.db.add(models.WorkWorkspaceRevision(workspace_id=self.basis.workspace_id,generation=1))
        self.db.commit()

    def state(self):
        self.db.expire_all()
        return self.db.get(models.WorkBasis,self.basis.id).state

    def test_assignee_round_trip_in_one_transaction_stays_revoked(self):
        self.db.execute(text('UPDATE tasks SET assignee_id=:owner WHERE id=:id'),{'owner':self.owner.id,'id':self.task.id})
        self.db.execute(text('UPDATE tasks SET assignee_id=:member WHERE id=:id'),{'member':self.member.id,'id':self.task.id})
        self.db.commit();self.assertEqual(self.state(),'REVOKED')

    def test_folder_round_trip_stays_revoked(self):
        target=self.db.get(models.TaskSubmissionTarget,self.task.id)
        target.folder_path='other';self.db.flush();target.folder_path='assignments/task-1';self.db.commit()
        self.assertEqual(self.state(),'REVOKED')

    def test_leave_and_rejoin_stays_revoked(self):
        row=self.db.query(models.TeamMember).filter_by(team_id=self.team.id,user_id=self.member.id).one()
        self.db.delete(row);self.db.flush()
        self.db.add(models.TeamMember(team_id=self.team.id,user_id=self.member.id,role='member'))
        self.db.commit();self.assertEqual(self.state(),'REVOKED')

    def test_role_change_and_restore_stays_revoked(self):
        row=self.db.query(models.TeamMember).filter_by(team_id=self.team.id,user_id=self.member.id).one()
        row.role='owner';self.db.flush();row.role='member';self.db.commit()
        self.assertEqual(self.state(),'REVOKED')

    def test_project_team_round_trip_stays_revoked(self):
        self.project.team_id=self.other_team.id;self.db.flush();self.project.team_id=self.team.id;self.db.commit()
        self.assertEqual(self.state(),'REVOKED')

    def test_workspace_generation_invalidates(self):
        advance_workspace(self.db,self.basis.workspace_id,2);self.db.commit()
        self.assertEqual(self.state(),'REVOKED')
        with self.assertRaises(StorageConflict):advance_workspace(self.db,self.basis.workspace_id,1)

    def test_task_done_then_reopened_not_revived(self):
        self.task.status=models.TaskStatus.DONE;self.db.flush();self.task.status=models.TaskStatus.TODO;self.db.commit()
        self.assertEqual(self.state(),'REVOKED')

    def test_task_delete_retains_basis(self):
        self.db.delete(self.task);self.db.commit()
        self.assertEqual(self.state(),'REVOKED')

    def test_rollback_rolls_back_invalidation(self):
        self.task.assignee_id=self.owner.id;self.db.flush();self.db.rollback()
        self.assertEqual(self.state(),'ACTIVE')

    def test_unrelated_title_change_does_not_revoke(self):
        self.task.title='新しい題名';self.db.commit()
        self.assertEqual(self.state(),'ACTIVE')

    def test_other_membership_does_not_revoke(self):
        row=self.db.query(models.TeamMember).filter_by(team_id=self.team.id,user_id=self.owner.id).one()
        row.role='member';self.db.commit();self.assertEqual(self.state(),'ACTIVE')

    def test_missing_trigger_refuses_feature(self):
        self.db.execute(text('DROP TRIGGER work_revoke_task_update'));self.db.commit()
        with self.assertRaises(StorageUnavailable):require_lifecycle(self.db)

    def test_postgres_sql_matches_explicit_migration(self):
        sql=(Path(__file__).parents[1]/'migrations'/'008_task_review_api.sql').read_text(encoding='utf-8')
        for statement in trigger_sql('postgresql'):self.assertIn(statement,sql)
        self.assertIn('INSERT INTO work_workspace_revisions',sql)
        self.assertTrue(models.WorkReviewPreview.__table__.info['requires_explicit_migration'])

    def test_empty_capture_is_not_no_change(self):
        with self.assertRaises(TaskReviewError) as caught:merge_task({}, {}, 'task', [])
        self.assertEqual(caught.exception.code,'EMPTY_CAPTURE_UNSUPPORTED')

    def test_nonterminal_status_does_not_revoke(self):
        self.task.status=models.TaskStatus.IN_PROGRESS;self.db.commit()
        self.assertEqual(self.state(),'ACTIVE')

    def test_cutover_revokes_preexisting_basis_without_deleting_it(self):
        # PostgreSQLの全DDLをSQLite適用済みとは扱わない。共通の切替UPDATEだけを確認する。
        statement="UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE';"
        sql=(Path(__file__).parents[1]/'migrations'/'008_task_review_api.sql').read_text(encoding='utf-8')
        self.assertIn(statement,sql)
        self.db.execute(text(statement));self.db.commit()
        self.assertEqual(self.state(),'REVOKED')
        self.assertEqual(self.db.query(models.WorkBasis).count(),1)

    def test_create_all_alone_does_not_enable_lifecycle(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        engine=create_engine('sqlite://')
        try:
            models.Base.metadata.create_all(engine)
            with Session(engine) as db:
                with self.assertRaises(StorageUnavailable):require_lifecycle(db)
        finally:engine.dispose()


class ReviewAPITests(unittest.TestCase):
    setUpClass=internal.ConversionTests.__dict__['setUpClass']
    tearDownClass=internal.ConversionTests.__dict__['tearDownClass']
    tearDown=internal.ConversionTests.tearDown
    headers=staticmethod(internal.ConversionTests.headers)
    decode_token=staticmethod(internal.ConversionTests.decode_token)
    register=internal.ConversionTests.register
    capture=internal.ConversionTests.capture
    start=internal.ConversionTests.start

    def setUp(self):
        internal.ConversionTests.setUp(self)
        with self.engine.begin() as connection:install_synthetic_lifecycle(connection)
        self.url=f'/api/teams/{self.team.id}/projects/{self.project.id}/tasks/{self.task.id}/work-bases'
        self.sessions=FakeSessions(self.workspaces)
        self.main.app.dependency_overrides[get_task_review_service]=lambda:self.service
        self.main.app.dependency_overrides[get_review_sessions]=lambda:self.sessions

    def request(self,method,suffix='',payload=None,uid='member'):
        return self.client.request(method,self.url+suffix,json=payload,headers=self.headers(uid))

    def prepared(self,files=None):
        basis=self.start();source=self.capture(basis,files)
        return basis,source,{'submission_id':source.id,'title':'変更を提出'}

    def preview(self,basis,payload):
        response=self.request('POST','/'+basis.id+'/preview',payload)
        self.assertEqual(response.status_code,200,response.json())
        return response.json()

    def test_start_current_and_no_arbitrary_inputs(self):
        result=self.request('POST',payload={});self.assertEqual(result.status_code,201,result.json())
        self.assertEqual(result.json()['state'],'ACTIVE')
        current=self.request('GET','/current').json();self.assertEqual(current['basis']['id'],result.json()['id'])
        bad=self.request('POST',payload={'base_commit':'a'*40,'path':'/arbitrary'})
        self.assertEqual(bad.status_code,422)

    def test_missing_auth_and_other_member_or_project(self):
        self.assertIn(self.client.post(self.url,json={}).status_code,(401,403))
        basis=self.start()
        self.assertEqual(self.request('GET','/'+basis.id,uid='owner').status_code,403)
        self.assertEqual(self.request('GET','/'+basis.id,uid='outsider').status_code,403)
        self.url=self.url.replace(f'/projects/{self.project.id}',f'/projects/{self.foreign.id}')
        self.assertEqual(self.request('GET','/'+basis.id).status_code,404)

    def test_preview_does_not_create_review_then_explicit_submit(self):
        basis,source,payload=self.prepared();view=self.preview(basis,payload)
        self.assertEqual(view['changes'],[{'path':self.prefix+'main.txt','change':'MODIFIED'}])
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)
        self.assertEqual(self.git.read_ref(('heads','main')),self.base)
        self.assertNotIn('changed',str(view));self.assertNotIn(source.commit_id,str(view))
        payload['confirmation_id']=view['confirmation_id']
        result=self.request('POST','/'+basis.id+'/change-request',payload)
        self.assertEqual(result.status_code,200,result.json());data=result.json()
        self.assertEqual((data['collection_state'],data['conversion_state']),('SUBMITTED','READY'))
        self.assertEqual((data['review_state'],data['integration_state']),('PENDING','NOT_INTEGRATED'))
        repeat=self.request('POST','/'+basis.id+'/change-request',payload)
        self.assertEqual(repeat.json()['change_request_id'],data['change_request_id'])
        check=self.request('GET',f'/{basis.id}/collections/{source.id}')
        self.assertEqual(check.json()['change_request_id'],data['change_request_id'])
        self.assertEqual(self.db.query(models.ChangeRequest).count(),1)

    def test_changed_title_requires_new_confirmation(self):
        basis,source,payload=self.prepared();view=self.preview(basis,payload)
        payload.update(confirmation_id=view['confirmation_id'],title='別の変更')
        result=self.request('POST','/'+basis.id+'/change-request',payload)
        self.assertEqual(result.status_code,409);self.assertEqual(result.json()['detail']['code'],'PREVIEW_CHANGED')
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_expired_confirmation_and_missing_confirmation(self):
        basis,source,payload=self.prepared();view=self.preview(basis,payload)
        row=self.db.get(models.WorkReviewPreview,view['confirmation_id']);row.expires_at=datetime.utcnow()-timedelta(seconds=1);self.db.commit()
        result=self.request('POST','/'+basis.id+'/change-request',dict(payload,confirmation_id=row.id))
        self.assertEqual(result.status_code,409)
        self.assertEqual(self.request('POST','/'+basis.id+'/change-request',payload).status_code,422)

    def test_no_change_is_separate_from_empty_capture(self):
        basis,source,payload=self.prepared({'main.txt':('100644',b'base')})
        view=self.preview(basis,payload);self.assertTrue(view['no_change'])
        result=self.request('POST','/'+basis.id+'/change-request',dict(payload,confirmation_id=view['confirmation_id']))
        self.assertEqual(result.json()['conversion_state'],'NO_CHANGE');self.assertIsNone(result.json()['change_request_id'])

    def test_reassigned_then_restored_basis_cannot_submit(self):
        basis,source,payload=self.prepared();view=self.preview(basis,payload)
        self.task.assignee_id=self.owner.id;self.db.flush();self.task.assignee_id=self.member.id;self.db.commit()
        result=self.request('POST','/'+basis.id+'/change-request',dict(payload,confirmation_id=view['confirmation_id']))
        self.assertEqual(result.status_code,409);self.assertEqual(self.db.query(models.ChangeRequest).count(),0)
        self.assertEqual(self.request('GET','/'+basis.id).json()['state'],'REVOKED')

    def test_session_dirty_unavailable_and_unverified(self):
        basis,source,payload=self.prepared()
        self.sessions.saved=False
        result=self.request('POST','/'+basis.id+'/preview',payload)
        self.assertEqual(result.json()['detail']['code'],'UNSAVED_CHANGES')
        self.main.app.dependency_overrides[get_review_sessions]=lambda:DeniedSessionVerifier()
        result=self.request('POST','/'+basis.id+'/preview',payload)
        self.assertEqual(result.status_code,503);self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_session_becomes_dirty_after_preview(self):
        basis,source,payload=self.prepared();view=self.preview(basis,payload);self.sessions.saved=False
        result=self.request('POST','/'+basis.id+'/change-request',dict(payload,confirmation_id=view['confirmation_id']))
        self.assertEqual(result.status_code,409);self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_deletion_candidates_and_partial_deletion(self):
        self.base_files[self.prefix+'remove.txt']=('100644',b'remove')
        tree=self.git.tree(self.base_files);self.base=self.git.run('commit-tree',tree,data=b'Fixture\n').decode().strip()
        self.git.run('update-ref','refs/heads/main',self.base)
        basis,source,payload=self.prepared()
        items=self.request('GET',f'/{basis.id}/deletion-candidates').json()['candidates']
        chosen=next(x for x in items if x['path'].endswith('remove.txt'))
        self.assertNotIn('.env',[x['path'].split('/')[-1] for x in items])
        evidence=self.request('POST',f'/{basis.id}/deletion-intents',{'submission_id':source.id,'candidate_id':chosen['id'],'confirmation_id':str(uuid4())})
        self.assertEqual(evidence.status_code,201,evidence.json())
        payload['deletion_ids']=[evidence.json()['deletion_id']]
        view=self.preview(basis,payload)
        self.assertIn({'path':self.prefix+'remove.txt','change':'DELETED'},view['changes'])

    def test_other_basis_confirmation_rejected(self):
        basis,source,payload=self.prepared();view=self.preview(basis,payload)
        another=self.start();other=self.capture(another)
        result=self.request('POST',f'/{another.id}/change-request',dict(payload,submission_id=other.id,confirmation_id=view['confirmation_id']))
        self.assertEqual(result.status_code,409)

    def test_main_advance_is_reported_not_rebased(self):
        basis,source,payload=self.prepared()
        tree=self.git.tree({'other':('100644',b'advanced')})
        advanced=self.git.run('commit-tree',tree,'-p',self.base,data=b'Advanced\n').decode().strip()
        self.git.run('update-ref','refs/heads/main',advanced)
        view=self.preview(basis,payload);self.assertTrue(view['main_differs'])
        result=self.request('POST',f'/{basis.id}/change-request',dict(payload,confirmation_id=view['confirmation_id']))
        self.assertTrue(result.json()['main_differs']);self.assertEqual(self.git.read_ref(('heads','main')),advanced)

    def test_collection_still_submitted_when_conversion_fails(self):
        basis,source,payload=self.prepared();view=self.preview(basis,payload)
        with patch('app.change_requests.register_review_snapshot',side_effect=RuntimeError('private failure')):
            response=self.request('POST',f'/{basis.id}/change-request',dict(payload,confirmation_id=view['confirmation_id']))
        self.assertEqual(response.status_code,503);self.assertNotIn('private failure',response.text)
        result=self.request('GET',f'/{basis.id}/collections/{source.id}').json()
        self.assertEqual((result['collection_state'],result['conversion_state']),('SUBMITTED','REVIEW_REQUIRED'))
        self.assertTrue(result['review_required'])

    def test_real_workspace_verifier_default_denies(self):
        self.main.app.dependency_overrides[get_task_review_service]=lambda:TaskReviewService(self.db,session_factory=self.git.session)
        self.assertEqual(self.request('POST',payload={}).status_code,503)
        self.assertEqual(self.db.query(models.WorkBasis).count(),0)

    def test_normal_user_cannot_select_test_dependencies(self):
        result=self.request('POST',payload={'provider':'fake','isDirty':False,'member_id':self.owner.id})
        self.assertEqual(result.status_code,422)

    def collection_payload(self,basis):
        session=str(uuid4());self.workspaces.sessions[session]=(basis.id,basis.workspace_id,basis.source_generation)
        permit_url=self.url.removesuffix('/work-bases')+'/submission-permits'
        result=self.client.post(permit_url,json={'editing_session_id':session},headers=self.headers('member'))
        self.assertEqual(result.status_code,201,result.json())
        return {'editing_session_id':session,'request_id':str(uuid4()),'permit':result.json()['permit'],'has_unsaved_changes':False}

    def test_collection_admission_binds_before_synthetic_git_save(self):
        from app.submission_git import GitSnapshotStore
        basis=self.start();payload=self.collection_payload(basis);fixture=self
        class ManagedFake:
            def ensure_ready(self):pass
            def collect(self,request):
                bound=fixture.db.get(models.WorkCaptureBinding,request.submission_id)
                fixture.assertEqual(bound.basis_id,basis.id)
                return GitSnapshotStore(fixture.git.session).save(request,lambda:{'main.txt':('100644',b'changed')},lambda:None)
            def verify(self,request,receipt):GitSnapshotStore(fixture.git.session)._verify(fixture.git,request)
        self.main.app.dependency_overrides[get_submission_provider]=lambda:ManagedFake()
        response=self.request('POST',f'/{basis.id}/collections',payload)
        self.assertEqual(response.status_code,202,response.json());self.assertEqual(response.json()['collection_state'],'SUBMITTED')
        repeat=self.request('POST',f'/{basis.id}/collections',payload)
        self.assertEqual(repeat.json()['submission_id'],response.json()['submission_id'])
        self.assertEqual(self.db.query(models.TaskSubmission).count(),1)
        review={'submission_id':response.json()['submission_id'],'title':'収集後の変更'}
        view=self.preview(basis,review)
        self.assertFalse(view['no_change'])

    def test_empty_collection_has_stable_reason_and_never_review(self):
        from app.submission_collection import EmptyTaskCollection
        basis=self.start();payload=self.collection_payload(basis)
        class EmptyFake:
            calls=0
            def ensure_ready(self):pass
            def collect(self,request):
                self.calls+=1;raise EmptyTaskCollection('not published')
        collector=EmptyFake();self.main.app.dependency_overrides[get_submission_provider]=lambda:collector
        result=self.request('POST',f'/{basis.id}/collections',payload)
        self.assertEqual(result.json()['error_code'],'EMPTY_CAPTURE_UNSUPPORTED')
        self.assertEqual(result.json()['collection_state'],'REVIEW_REQUIRED')
        repeat=self.request('POST',f'/{basis.id}/collections',payload)
        self.assertEqual(repeat.json()['error_code'],'EMPTY_CAPTURE_UNSUPPORTED');self.assertEqual(collector.calls,1)
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_default_collector_refuses_before_permit_consumption(self):
        basis=self.start();payload=self.collection_payload(basis)
        self.main.app.dependency_overrides[get_submission_provider]=lambda:DeniedSubmissionProvider()
        result=self.request('POST',f'/{basis.id}/collections',payload)
        self.assertEqual(result.status_code,503)
        self.assertIsNone(self.db.query(models.SubmissionPermit).one().consumed_at)
        self.assertEqual(self.db.query(models.TaskSubmission).count(),0)

    def test_workspace_generation_change_prevents_preview(self):
        basis,source,payload=self.prepared()
        advance_workspace(self.db,basis.workspace_id,2);self.db.commit()
        self.assertEqual(self.request('POST',f'/{basis.id}/preview',payload).status_code,409)
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_collection_cannot_be_moved_to_another_basis(self):
        basis,source,payload=self.prepared();another=self.start()
        self.assertEqual(self.request('POST',f'/{another.id}/preview',payload).status_code,409)

    def test_current_get_does_not_implicitly_submit(self):
        basis=self.start()
        for _ in range(2):self.assertEqual(self.request('GET','/current').status_code,200)
        self.assertEqual(self.db.query(models.TaskSubmission).count(),0)
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_deletion_confirmation_retries_return_same_evidence(self):
        basis,source,payload=self.prepared()
        candidate=next(iter(self.service.deletion_candidates(self.member,basis.id)))
        body={'submission_id':source.id,'candidate_id':candidate,'confirmation_id':str(uuid4())}
        first=self.request('POST',f'/{basis.id}/deletion-intents',body)
        again=self.request('POST',f'/{basis.id}/deletion-intents',body)
        self.assertEqual(first.status_code,201);self.assertEqual(again.json(),first.json())
        body['confirmation_id']=str(uuid4())
        self.assertEqual(self.request('POST',f'/{basis.id}/deletion-intents',body).status_code,409)

    def test_revocation_during_collection_prevents_publication(self):
        from app.submission_git import GitSnapshotStore
        basis=self.start();payload=self.collection_payload(basis);fixture=self
        class RevokingFake:
            def ensure_ready(self):pass
            def collect(self,request):
                result=GitSnapshotStore(fixture.git.session).save(request,lambda:{'main.txt':('100644',b'changed')},lambda:None)
                fixture.task.assignee_id=fixture.owner.id;fixture.db.flush()
                fixture.task.assignee_id=fixture.member.id;fixture.db.commit()
                return result
            def verify(self,*args):pass
        self.main.app.dependency_overrides[get_submission_provider]=lambda:RevokingFake()
        result=self.request('POST',f'/{basis.id}/collections',payload)
        self.assertEqual(result.json()['collection_state'],'REVIEW_REQUIRED')
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)
        self.assertEqual(self.db.get(models.WorkBasis,basis.id).state,'REVOKED')

    def test_existing_change_request_detail_diff_approve_and_request_changes(self):
        basis,source,payload=self.prepared();view=self.preview(basis,payload)
        result=self.request('POST',f'/{basis.id}/change-request',dict(payload,confirmation_id=view['confirmation_id']))
        crid=result.json()['change_request_id']
        prefix=f'/api/teams/{self.team.id}/projects/{self.project.id}/change-requests'
        self.assertEqual(self.client.get(prefix,headers=self.headers('member')).status_code,200)
        self.assertEqual(self.client.get(prefix+f'/{crid}',headers=self.headers('member')).json()['id'],crid)
        self.main.app.dependency_overrides[get_project_storage]=lambda:None
        with patch('app.change_requests.ReviewGit',return_value=self.git),patch('app.change_request_reviews.ReviewGit',return_value=self.git):
            self.assertEqual(self.client.get(prefix+f'/{crid}/diff',headers=self.headers('owner')).status_code,200)
            approved=self.client.post(prefix+f'/{crid}/approve',json={},headers=self.headers('owner'))
            self.assertEqual(approved.status_code,200)
            another=self.capture(basis,{'main.txt':('100644',b'next')})
            second=dict(payload,submission_id=another.id,title='修正後')
            view=self.preview(basis,second)
            result=self.request('POST',f'/{basis.id}/change-request',dict(second,confirmation_id=view['confirmation_id']))
            cr2=result.json()['change_request_id']
            response=self.client.post(prefix+f'/{cr2}/request-changes',json={'reason':'修正をお願いします'},headers=self.headers('owner'))
            self.assertEqual(response.status_code,200)
        self.assertEqual(self.db.get(models.ChangeRequest,crid).review.decision,'APPROVED')
        self.assertEqual(self.db.get(models.ChangeRequest,cr2).review.decision,'CHANGES_REQUESTED')
