"""認可証拠は合成鍵・模擬session・SQLiteメモリDBのみで検証する。"""
import copy
from dataclasses import asdict,replace
from datetime import datetime
from uuid import uuid4
import unittest
from unittest.mock import patch

import test_submissions as fixtures
from app import models
from app.submission_authorization import BackendAuthorizationIssuer,CollectorAuthorizationVerifier,VerifiedSession,DeniedSessionVerifier
from app.submission_rpc import CollectorRPCClient,CollectorRPCServer,RPCKeys,MemoryReplayLedger,pack,MAX_REQUEST
from app.submission_provider import ProviderUnavailable,SubmissionCollection
from app.submission_scope import SubmissionTarget
from submission_snapshot_fakes import FakeTaskOps


class FakeSessions:
    """検証専用。実code-serverのdirty状態を保証するものではない。"""
    def __init__(self,evidence):self.evidence=evidence
    def verify(self,*args):return self.evidence


class AuthorizationTests(unittest.TestCase):
    setUpClass=fixtures.SubmissionAPITests.__dict__['setUpClass']
    tearDownClass=fixtures.SubmissionAPITests.__dict__['tearDownClass']
    tearDown=fixtures.SubmissionAPITests.tearDown
    headers=staticmethod(fixtures.SubmissionAPITests.headers)
    decode_token=staticmethod(fixtures.SubmissionAPITests.decode_token)
    register=fixtures.SubmissionAPITests.register
    post=fixtures.SubmissionAPITests.post
    permit=fixtures.SubmissionAPITests.permit
    payload=fixtures.SubmissionAPITests.payload

    def setUp(self):
        fixtures.SubmissionAPITests.setUp(self)
        self.body=self.payload();permit=self.db.query(models.SubmissionPermit).one()
        permit.consumed_at=datetime.utcnow()
        self.row=models.TaskSubmission(id=str(uuid4()),student_id=self.member.id,team_id=self.team.id,
            project_id=self.project.id,task_id=self.task.id,request_id=self.body['request_id'],
            editing_session_id=self.session,permit_id=permit.id,permit_digest=permit.token_digest,
            folder_path='assignments/task-1',state='PROCESSING',created_at=datetime.utcnow())
        self.db.add(self.row);self.db.commit()
        self.target=SubmissionTarget(self.team.id,self.project.id,self.task.id,self.member.id,'assignments/task-1')
        self.request=SubmissionCollection(self.row.id,self.row.request_id,self.session,self.target)
        self.now=1000;self.session_evidence=VerifiedSession(self.target,self.session,1,1100,True)
        self.sessions=FakeSessions(self.session_evidence)
        # 公開された合成鍵。本番利用は禁止。RPC鍵と認可鍵は別々にする。
        self.key=b'synthetic-student-authorization-key'
        self.keys=RPCKeys(b'synthetic-request-key'*2,b'synthetic-response-key'*2)
        self.issuer=BackendAuthorizationIssuer(self.db,self.member,self.key,'collector-a',lambda t:1,sessions=self.sessions,clock=lambda:self.now)
        self.verifier=CollectorAuthorizationVerifier(self.key,'collector-a',self.target,1,sessions=self.sessions,clock=lambda:self.now)
        self.calls=[]
        def collect(*args):self.calls.append(args);return {'main.txt':('100644',b'fixture')}
        self.server=CollectorRPCServer(self.keys,MemoryReplayLedger(),{self.target:(1,collect)},self.verifier,clock=lambda:self.now)

    def signed(self):
        request={'version':2,'operation':'COLLECT_SAVED_TASK','submission_id':self.request.submission_id,
            'request_id':self.request.request_id,'editing_session_id':self.session,'target':asdict(self.target),
            'source_generation':1,'nonce':str(uuid4()),'issued_at':1000,'expires_at':1090,
            'client_service':'submission-manager','server_service':'task-collector','authorization':None}
        request['authorization']=self.issuer.issue(request)
        return request

    def handle(self,request):return self.server.handle(pack(request,self.keys.request_key,b'REQUEST\0',MAX_REQUEST))

    def test_valid_issuer_client_server_integration(self):
        case=self
        class Transport:
            def exchange(self,frame,**kwargs):return case.server.handle(frame)
        client=CollectorRPCClient(self.keys,MemoryReplayLedger(),lambda t:1,Transport(),clock=lambda:1000,authorization_issuer=self.issuer)
        self.assertEqual(client.collect(self.request),{'main.txt':('100644',b'fixture')})
        self.assertEqual(len(self.calls),1)

    def test_missing_authenticated_actor(self):
        self.issuer.actor=None
        with self.assertRaises(ProviderUnavailable):self.signed()
        self.assertEqual(self.calls,[])

    def test_membership_revoked_before_issue(self):
        self.db.query(models.TeamMember).filter_by(team_id=self.team.id,user_id=self.member.id).delete();self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.signed()

    def test_task_assignment_changed(self):
        self.task.assignee_id=self.owner.id;self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.signed()

    def test_registered_target_changed(self):
        self.db.query(models.TaskSubmissionTarget).one().folder_path='other';self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.signed()

    def test_consumed_permit_required(self):
        self.db.query(models.SubmissionPermit).one().consumed_at=None;self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.signed()

    def test_permit_expired(self):
        permit=self.db.query(models.SubmissionPermit).one();permit.created_at=datetime(1970,1,1);permit.expires_at=datetime(1970,1,1,0,1);self.db.commit()
        with self.assertRaises(ProviderUnavailable):self.signed()

    def test_student_project_task_transfer_rejected_before_collect(self):
        original=self.signed()
        for key in ('student_id','team_id','project_id','task_id'):
            request=copy.deepcopy(original);request['target'][key]+=100
            with self.assertRaises(ProviderUnavailable):self.handle(request)
        self.assertEqual(self.calls,[])

    def test_generation_transfer_rejected(self):
        request=self.signed();request['source_generation']=2
        with self.assertRaises(ProviderUnavailable):self.handle(request)
        self.assertEqual(self.calls,[])

    def test_wrong_collector_rejected(self):
        request=self.signed();self.verifier.collector_id='collector-b'
        with self.assertRaises(ProviderUnavailable):self.handle(request)

    def test_tampered_authorization_rejected(self):
        request=self.signed();request['authorization']['claims']['expires_at']+=1
        with self.assertRaises(ProviderUnavailable):self.handle(request)
        self.assertEqual(self.calls,[])

    def test_expired_authorization(self):
        request=self.signed();self.now=1031
        with self.assertRaises(ProviderUnavailable):self.handle(request)

    def test_session_id_transfer_rejected(self):
        request=self.signed();request['editing_session_id']=str(uuid4())
        with self.assertRaises(ProviderUnavailable):self.handle(request)

    def test_revoked_session_after_issue(self):
        request=self.signed();self.sessions.evidence=replace(self.session_evidence,expires_at=999)
        with self.assertRaises(ProviderUnavailable):self.handle(request)
        self.assertEqual(self.calls,[])

    def test_default_session_verifier_denies(self):
        self.issuer.sessions=DeniedSessionVerifier()
        with self.assertRaises(ProviderUnavailable):self.signed()

    def test_collector_default_session_verifier_denies(self):
        request=self.signed();self.verifier.sessions=DeniedSessionVerifier()
        with self.assertRaises(ProviderUnavailable):self.handle(request)

    def test_dirty_session_denies(self):
        self.sessions.evidence=replace(self.session_evidence,saved=False)
        with self.assertRaises(ProviderUnavailable):self.signed()
        self.assertEqual(self.calls,[])

    def test_validator_exception_denies(self):
        request=self.signed()
        with patch.object(self.sessions,'verify',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(ProviderUnavailable):self.handle(request)
        self.assertEqual(self.calls,[])

    def test_duplicate_request_never_recollects(self):
        request=self.signed();self.handle(request)
        with self.assertRaises(ProviderUnavailable):self.handle(request)
        self.assertEqual(len(self.calls),1)

    def test_ledger_failure_prevents_collection(self):
        request=self.signed()
        with patch.object(self.server.ledger,'claim',side_effect=ProviderUnavailable()):
            with self.assertRaises(ProviderUnavailable):self.handle(request)
        self.assertEqual(self.calls,[])

    def test_expiry_after_collection_refuses_result(self):
        request=self.signed()
        def late(*args):self.calls.append(args);self.now=1040;return {'main.txt':('100644',b'fixture')}
        self.server.sources[self.target]=(1,late)
        with self.assertRaises(ProviderUnavailable):self.handle(request)
        with self.assertRaises(ProviderUnavailable):self.handle(request)
        self.assertEqual(len(self.calls),1)

    def test_backend_provider_factory_connects_without_local_source(self):
        from app.submission_authorization import authorized_rpc_provider
        case=self
        class Transport:
            def exchange(self,frame,**kwargs):return case.server.handle(frame)
        class Store:
            # Git永続化そのものはStage 3で検証済み。この試験は接続順序だけを観測する。
            def save(self,request,collect,authorize):
                case.assertTrue(authorize() is None)  # ManagedSnapshotProviderのcheckは正常時None。
                result=collect();authorize();return result
        provider=authorized_rpc_provider(self.db,self.member,rpc_keys=self.keys,
            authorization_key=self.key,collector_id='collector-a',target=self.target,generation=1,
            store=Store(),transport=Transport(),sessions=self.sessions,ledger=MemoryReplayLedger(),clock=lambda:1000)
        self.assertEqual(provider.collect(self.request),{'main.txt':('100644',b'fixture')})

    def test_factory_without_session_refuses(self):
        from app.submission_authorization import authorized_rpc_provider
        with self.assertRaises(ProviderUnavailable):authorized_rpc_provider(self.db,self.member,
            rpc_keys=self.keys,authorization_key=self.key,collector_id='collector-a',target=self.target,
            generation=1,store=object(),ledger=MemoryReplayLedger())

    def test_factory_rejects_shared_authentication_key(self):
        from app.submission_authorization import authorized_rpc_provider
        with self.assertRaises(ProviderUnavailable):authorized_rpc_provider(self.db,self.member,
            rpc_keys=self.keys,authorization_key=self.keys.request_key,collector_id='collector-a',target=self.target,
            generation=1,store=object(),sessions=self.sessions,ledger=MemoryReplayLedger())
