"""製品DB認可・HMAC・永続台帳・RPCを接続する。Linux FSだけは合成実装。"""
from dataclasses import asdict,replace
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_work_environment as fixtures
import test_submission_rpc_provider as collection_fixtures
from app import models
from app.managed_process import WorkerUnknown
from app.submission_provider import ProviderUnavailable
from app.submission_rpc import RPCKeys,pack,unpack
from app.submission_scope import SubmissionTarget
from app.workspace_rpc import (RemoteWorkspaces,WorkspaceRPCServer,WorkspaceAuthorizationIssuer,
    WorkspaceAuthorizationVerifier,REQUEST_DOMAIN,MAX_FRAME,authorization_signature)
from app.workspace_journal import WorkspaceJournal
from app.workspace_files import MappedWorkspaces


def guard(path):
    info=path.stat();return info.st_dev,info.st_ino


class NativeFixture:
    """OS境界だけを模擬。提出認可や署名、台帳・DB処理の許可モックではない。"""
    def __init__(self):self.initialized=[];self.proofs={};self.fail=False;self.exists=True
    def run(self,job):
        if job['action']=='INITIALIZE':
            self.initialized.append(job['basis_id'])
            if self.fail:raise WorkerUnknown('synthetic termination')
            self.proofs[job['basis_id']]={'basis_id':job['basis_id'],'target':job['target'],
                'base_commit':job['base_commit'],'workspace_id':str(uuid4()),'source_generation':1}
            return {'proof':self.proofs[job['basis_id']]}
        return {'verified':self.exists and self.proofs.get(job['basis_id'])==job['proof'],
                'unchanged':self.exists and self.proofs.get(job['basis_id'])==job['proof']}


class SnapshotPathTests(unittest.TestCase):
    def test_git_project_dotfiles_are_preserved(self):
        from app.workspace_rpc import encode_snapshot,decode_snapshot
        files={'.gitignore':('100644',b'synthetic\n'),'task/main.py':('100755',b'synthetic')}
        self.assertEqual(decode_snapshot(encode_snapshot(files)),files)

    def test_git_admin_and_traversal_paths_are_rejected(self):
        from app.workspace_rpc import encode_snapshot
        for path in ('.git/config','../other','/absolute','a/../../b'):
            with self.subTest(path=path):
                with self.assertRaises(Exception):encode_snapshot({path:('100644',b'synthetic')})


class WorkspaceRPCTests(unittest.TestCase):
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

    def setUp(self):
        fixtures.EnvironmentTests.setUp(self)
        self.target=SubmissionTarget(self.team.id,self.project.id,self.task.id,self.member.id,'assignments/task-1')
        self.keys=RPCKeys(b'synthetic-workspace-request-key'*2,b'synthetic-workspace-response-key'*2)
        self.auth=b'synthetic-workspace-authorization-key'*2
        self.journal=WorkspaceJournal.provision(Path(self.temp.name)/'workspace.sqlite',guard=guard)
        self.native=NativeFixture();self.frames=[]
        self.server=WorkspaceRPCServer(self.keys,self.target,'/synthetic-fixed-root',1000,1000,self.journal,
            WorkspaceAuthorizationVerifier(self.auth,self.target),runner=self.native)
        outer=self
        class Transport:
            disconnect=False
            def exchange(self,frame,**kwargs):
                outer.frames.append(frame);response=outer.server.handle(frame)
                if self.disconnect:raise ConnectionError('synthetic response loss')
                return response
        self.transport=Transport()
        self.remote=RemoteWorkspaces(self.keys,self.transport,WorkspaceAuthorizationIssuer(self.db,self.member,self.auth),
            member_id=self.member.id,project_id=self.project.id)
        self.service.workspaces=MappedWorkspaces({(self.member.id,self.project.id):self.remote})

    def prepare(self):
        key=str(uuid4());response=self.request('POST','/environment',{'request_id':key})
        return key,response

    def test_real_issuer_rpc_and_durable_admission_prepare(self):
        key,response=self.prepare();self.assertEqual(response.status_code,200)
        row=self.db.query(models.WorkEnvironmentOperation).filter_by(request_id=key).one()
        self.assertEqual(row.base_commit,self.base);self.assertEqual(row.state,'CONFIRMED')
        self.assertEqual(self.native.initialized,[row.result_basis_id])
        self.assertEqual(self.journal.lookup(row.result_basis_id)['state'],'COMPLETE')
        self.assertEqual(self.git.read_ref(('heads','main')),self.base)

    def test_repeated_http_request_does_not_initialize_again(self):
        key,response=self.prepare();again=self.request('POST','/environment',{'request_id':key})
        self.assertEqual(response.json(),again.json());self.assertEqual(len(self.native.initialized),1)

    def test_response_loss_lookup_never_reinitializes_or_confirms_database(self):
        self.transport.disconnect=True;key,response=self.prepare();self.assertEqual(response.status_code,503)
        self.transport.disconnect=False
        before=self.db.query(models.WorkBasis).count()
        result=self.request('GET','/environment/requests/'+key)
        self.assertEqual(result.status_code,200);self.assertEqual(result.json()['state'],'PROCESSING')
        self.assertEqual(result.json()['initialization_state'],'COMPLETE')
        self.assertEqual(self.db.query(models.WorkBasis).count(),before);self.assertEqual(len(self.native.initialized),1)

    def test_worker_failure_is_durable_unknown_after_reopen(self):
        self.native.fail=True;key,response=self.prepare();self.assertEqual(response.status_code,503)
        op=self.db.query(models.WorkEnvironmentOperation).filter_by(request_id=key).one()
        reopened=WorkspaceJournal(self.journal.path,guard=guard)
        self.assertEqual(reopened.lookup(op.result_basis_id)['state'],'UNKNOWN')
        self.request('POST','/environment',{'request_id':key});self.assertEqual(len(self.native.initialized),1)

    def test_different_content_cannot_reuse_initialization(self):
        self.prepare();request=unpack(self.frames[0],self.keys.request_key,REQUEST_DOMAIN,MAX_FRAME)
        request['files']={};request['nonce']=str(uuid4());request['authorization']=authorization_signature(self.auth,request)
        with self.assertRaises(ProviderUnavailable):self.server.handle(pack(request,self.keys.request_key,REQUEST_DOMAIN,MAX_FRAME))
        self.assertEqual(len(self.native.initialized),1)

    def test_other_member_task_project_and_generation_are_rejected(self):
        self.prepare();original=unpack(self.frames[0],self.keys.request_key,REQUEST_DOMAIN,MAX_FRAME)
        for name in ('student_id','project_id','task_id'):
            request={**original,'target':{**original['target'],name:999}}
            request['authorization']=authorization_signature(self.auth,request)
            with self.subTest(name=name):
                with self.assertRaises(ProviderUnavailable):self.server.handle(pack(request,self.keys.request_key,REQUEST_DOMAIN,MAX_FRAME))
        request={**original,'generation':2};request['authorization']=authorization_signature(self.auth,request)
        with self.assertRaises(ProviderUnavailable):self.server.handle(pack(request,self.keys.request_key,REQUEST_DOMAIN,MAX_FRAME))
        self.assertEqual(len(self.native.initialized),1)

    def test_wrong_hmac_before_worker(self):
        key=str(uuid4())
        with patch.object(self.transport,'exchange',side_effect=lambda frame,**kw:self.server.handle(frame[:-1]+b'x')):
            self.assertEqual(self.request('POST','/environment',{'request_id':key}).status_code,503)
        self.assertEqual(self.native.initialized,[])

    def test_missing_proof_is_not_verified(self):
        self.prepare();self.native.exists=False
        basis=self.db.query(models.WorkBasis).one()
        self.assertFalse(self.service.workspaces.verify(__import__('app.task_review',fromlist=['WorkspaceInitialization']).WorkspaceInitialization(
            basis.id,self.target,basis.base_commit,basis.workspace_id,basis.source_generation)))
        self.assertEqual(len(self.native.initialized),1)

    def test_journal_commit_ack_loss_keeps_complete(self):
        original=self.journal.finish;lost=[]
        def finish(*args):
            original(*args)
            if not lost:lost.append(True);raise RuntimeError('synthetic commit acknowledgement loss')
        with patch.object(self.journal,'finish',side_effect=finish):key,response=self.prepare()
        self.assertEqual(response.status_code,503)
        op=self.db.query(models.WorkEnvironmentOperation).filter_by(request_id=key).one()
        self.assertEqual(self.journal.lookup(op.result_basis_id)['state'],'COMPLETE')
        self.assertEqual(len(self.native.initialized),1)

    def test_default_authority_still_refuses_editor_and_submission(self):
        from app.work_environment import EditingSessions
        from app.editor_authority import IndependentEditorAuthority
        self.runtime.sessions=EditingSessions(self.db,IndependentEditorAuthority())
        _,response=self.prepare();self.assertEqual(response.status_code,200)
        basis=response.json()['basis_id']
        result=self.request('POST','/'+basis+'/editing-sessions',{})
        self.assertEqual(result.status_code,503)
        self.assertFalse(self.request('GET','/environment').json()['launch_enabled'])

    def test_lookup_missing_is_unknown(self):
        result=self.request('GET','/environment/requests/'+str(uuid4()))
        self.assertEqual(result.json()['state'],'UNKNOWN');self.assertEqual(self.native.initialized,[])

    def test_missing_bootstrap_rejects_without_reading_keys(self):
        from app.workspace_bootstrap import configured_workspace_server
        with patch('app.workspace_bootstrap.private_bytes',side_effect=AssertionError('must not read')):
            with self.assertRaises(ProviderUnavailable):configured_workspace_server({})


class IsolatedCollectionTests(unittest.TestCase):
    setUpClass=collection_fixtures.RPCProviderConnectionTests.__dict__['setUpClass']
    tearDownClass=collection_fixtures.RPCProviderConnectionTests.__dict__['tearDownClass']
    tearDown=collection_fixtures.RPCProviderConnectionTests.tearDown
    headers=staticmethod(collection_fixtures.RPCProviderConnectionTests.headers)
    decode_token=staticmethod(collection_fixtures.RPCProviderConnectionTests.decode_token)
    register=collection_fixtures.RPCProviderConnectionTests.register
    post=collection_fixtures.RPCProviderConnectionTests.post
    permit=collection_fixtures.RPCProviderConnectionTests.permit
    payload=collection_fixtures.RPCProviderConnectionTests.payload
    make_provider=collection_fixtures.RPCProviderConnectionTests.make_provider

    def setUp(self):collection_fixtures.RPCProviderConnectionTests.setUp(self)

    def test_timeout_consumes_ledger_and_never_writes_incomplete_git(self):
        from app.submission_collector import IsolatedRootCollector
        from app.submission_ledger import SQLiteReplayLedger
        calls=[]
        class Runner:
            def run(self,job):calls.append(job['action']);raise WorkerUnknown('synthetic killed worker')
        ledger=SQLiteReplayLedger.provision(Path(self.temp.name)/'collection.sqlite',guard=guard)
        self.server.ledger=ledger
        self.server.sources[self.target]=(1,IsolatedRootCollector('/fixed-synthetic-root',self.target,1000,runner=Runner()))
        body=self.payload();result=self.post('/submissions',body).json()
        self.assertEqual(result['state'],'REVIEW_REQUIRED');self.assertEqual(calls,['COLLECT'])
        self.assertIsNone(self.git.submission_ref(result['id']))
        self.assertIsNone(self.git.journal_read(result['id'],'prepared'))
        reopened=SQLiteReplayLedger(ledger.path,guard=guard)
        with self.assertRaises(ProviderUnavailable):reopened.claim(result['id'])
        self.post('/submissions',body);self.assertEqual(calls,['COLLECT'])
