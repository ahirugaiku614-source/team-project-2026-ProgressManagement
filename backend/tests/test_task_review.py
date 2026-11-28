"""合成workspace証拠・一時Git・SQLiteメモリDBで内部接続を検証する。"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import sqlite3
from uuid import uuid4
import unittest
from unittest.mock import patch

import test_submissions as fixtures
from submission_snapshot_fakes import PortableGitFixture
from app import models
from app.task_review import TaskReviewService,WorkspaceInitialization,merge_task
from app.submission_git import GitSnapshotStore
from app.submission_scope import SubmissionTarget
from app.submission_provider import SubmissionCollection
from app.project_storage import StorageConflict
from app.change_request_reviews import verify_snapshot
from app.change_requests import row_manifest


class ReviewFixture(PortableGitFixture):
    """LinuxのFD/ロックだけを置換。Gitオブジェクトと既存検証器は本物を使う。"""
    def __init__(self,directory):
        self.review_metadata=True
        super().__init__(directory)
    def read_ref(self,parts):
        value=self.run('for-each-ref','--format=%(objectname)','refs/'+'/'.join(parts)).strip()
        return value.decode() if value else None
    def basis_ref(self,key):return self.read_ref(('work-bases',key))
    def pin_basis(self,key,commit):self.run('update-ref','refs/work-bases/'+key,commit,'0'*40)
    def manifest(self,key):return self.journal_read(key,'review')
    def save_manifest(self,manifest):
        old=self.manifest(manifest['ref_key'])
        if old is None:self.journal_create(manifest['ref_key'],'review',manifest)
        elif old!=manifest:raise StorageConflict('Fixture manifest mismatch')
    def ensure_refs(self,manifest):
        for part,field in (('base','base_commit'),('head','snapshot_commit')):
            parts=('change-requests',manifest['ref_key'],part);old=self.read_ref(parts)
            if old is not None and old!=manifest[field]:raise StorageConflict('Fixture ref mismatch')
        for part,field in (('base','base_commit'),('head','snapshot_commit')):
            parts=('change-requests',manifest['ref_key'],part)
            if self.read_ref(parts) is None:self.run('update-ref','refs/'+'/'.join(parts),manifest[field],'0'*40)
    def verify_refs(self,manifest):
        for part,field in (('base','base_commit'),('head','snapshot_commit')):
            if self.read_ref(('change-requests',manifest['ref_key'],part))!=manifest[field]:raise StorageConflict('Fixture ref missing')
    @contextmanager
    def locked_review_refs(self,key):
        with self.lock:yield


class FakeWorkspaces:
    """実workspaceは一切初期化しない。検証済み証拠の契約だけを模擬する。"""
    def __init__(self):self.proofs={};self.sessions={};self.valid=True
    def initialize(self,target,base,basis_id):
        proof=WorkspaceInitialization(basis_id,target,base,str(uuid4()),1)
        self.proofs[basis_id]=proof;return proof
    def verify(self,proof):return self.valid and self.proofs.get(proof.basis_id)==proof
    def verify_collection(self,basis,row):return self.valid and self.sessions.get(row.editing_session_id)==(basis.id,basis.workspace_id,basis.source_generation)


class ConversionTests(unittest.TestCase):
    setUpClass=fixtures.SubmissionAPITests.__dict__['setUpClass']
    tearDownClass=fixtures.SubmissionAPITests.__dict__['tearDownClass']
    tearDown=fixtures.SubmissionAPITests.tearDown
    headers=staticmethod(fixtures.SubmissionAPITests.headers)
    decode_token=staticmethod(fixtures.SubmissionAPITests.decode_token)
    register=fixtures.SubmissionAPITests.register
    def setUp(self):
        fixtures.SubmissionAPITests.setUp(self)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.git=ReviewFixture(self.temp.name);self.workspaces=FakeWorkspaces()
        p=patch.object(self.git,'capture',side_effect=AssertionError('Workspace recapture is prohibited'))
        p.start();self.addCleanup(p.stop)
        self.service=TaskReviewService(self.db,workspaces=self.workspaces,session_factory=self.git.session)
        self.prefix='assignments/task-1/'
        self.base_files={self.prefix+'main.txt':('100755',b'base'),self.prefix+'.env':('100644',b'excluded'),
                         'outside.sh':('100755',b'outside')}
        tree=self.git.tree(self.base_files)
        self.base=self.git.run('commit-tree',tree,data=b'Initial fixture\n').decode().strip()
        self.git.run('update-ref','refs/heads/main',self.base,'0'*40)

    def start(self):return self.service.start(self.member,self.team.id,self.project.id,self.task.id)

    def capture(self,basis,files=None,bind=True):
        files=files if files is not None else {'main.txt':('100644',b'changed')}
        session=str(uuid4());self.workspaces.sessions[session]=(basis.id,basis.workspace_id,basis.source_generation)
        permit=models.SubmissionPermit(id=str(uuid4()),token_digest=uuid4().hex*2,student_id=self.member.id,
            team_id=self.team.id,project_id=self.project.id,task_id=self.task.id,editing_session_id=session,
            folder_path='assignments/task-1',purpose='TASK_SUBMISSION',created_at=datetime(2020,1,1),
            expires_at=datetime(2090,1,1),consumed_at=datetime.utcnow())
        self.db.add(permit);self.db.flush()
        row=models.TaskSubmission(id=str(uuid4()),student_id=self.member.id,team_id=self.team.id,
            project_id=self.project.id,task_id=self.task.id,request_id=str(uuid4()),editing_session_id=session,
            permit_id=permit.id,permit_digest=permit.token_digest,folder_path='assignments/task-1',state='PROCESSING',created_at=datetime.utcnow())
        self.db.add(row);self.db.commit()
        if bind:self.service.bind_collection(self.member,basis.id,row.id)
        target=SubmissionTarget(self.team.id,self.project.id,self.task.id,self.member.id,'assignments/task-1')
        receipt=GitSnapshotStore(self.git.session).save(SubmissionCollection(row.id,row.request_id,session,target),lambda:files,lambda:None)
        row.state='SUBMITTED';row.git_ref=receipt.git_ref;row.commit_id=receipt.commit_id
        row.file_count=receipt.file_count;row.total_bytes=receipt.total_bytes;row.submitted_at=datetime.utcnow();self.db.commit()
        return row

    def test_basis_pinned_without_main_change(self):
        basis=self.start()
        self.assertEqual(basis.base_commit,self.base);self.assertEqual(self.git.basis_ref(basis.id),self.base)
        self.assertEqual(self.git.read_ref(('heads','main')),self.base)

    def test_missing_initialization_proof_rejected(self):
        self.workspaces.valid=False
        with self.assertRaises(StorageConflict):self.start()

    def test_mismatched_initialization_proof_rejected(self):
        original=self.workspaces.initialize
        self.workspaces.initialize=lambda *args:replace(original(*args),base_commit='f'*40)
        with self.assertRaises(StorageConflict):self.start()

    def test_missing_basis_ref_not_recreated(self):
        basis=self.start();self.git.run('update-ref','-d','refs/work-bases/'+basis.id)
        with self.assertRaises(StorageConflict):self.service.deletion_candidates(self.member,basis.id)
        self.assertIsNone(self.git.basis_ref(basis.id))

    def test_reassignment_revokes_without_deleting_history(self):
        basis=self.start();self.task.assignee_id=self.owner.id;self.db.commit()
        with self.assertRaises(Exception):self.service.deletion_candidates(self.member,basis.id)
        self.assertEqual(self.db.get(models.WorkBasis,basis.id).state,'REVOKED')
        self.assertIsNotNone(self.git.basis_ref(basis.id))

    def test_end_retains_basis(self):
        basis=self.start();self.service.end(self.member,basis.id)
        self.assertIsNotNone(self.git.basis_ref(basis.id))
        with self.assertRaises(StorageConflict):self.service.deletion_candidates(self.member,basis.id)

    def test_conversion_preserves_outside_modes_and_original_commits(self):
        basis=self.start();source=self.capture(basis,{'main.txt':('100644',b'changed'),'new.txt':('100644',b'new')})
        source_commit=source.commit_id
        result=self.service.convert(self.member,basis.id,source.id,'変更内容')
        self.assertEqual(result.state,'READY')
        row=self.db.get(models.ChangeRequest,result.change_request_id);verify_snapshot(self.git,row)
        entries=self.git.entries(row.snapshot_commit)
        self.assertEqual(entries['outside.sh'],self.git.entries(self.base)['outside.sh'])
        self.assertEqual(entries[self.prefix+'.env'],self.git.entries(self.base)[self.prefix+'.env'])
        self.assertEqual(entries[self.prefix+'main.txt'][0],'100755')
        self.assertEqual(entries[self.prefix+'new.txt'][0],'100644')
        self.assertEqual(self.git.run('rev-list','--parents','-n','1',row.snapshot_commit).split(),[row.snapshot_commit.encode(),self.base.encode()])
        self.assertEqual(self.git.submission_ref(source.id),source_commit)
        self.assertEqual(self.git.read_ref(('heads','main')),self.base)
        diff=self.git.diff(row_manifest(row))
        self.assertEqual({f['path'] for f in diff['files']},{self.prefix+'main.txt',self.prefix+'new.txt'})

    def test_no_change_no_review(self):
        basis=self.start();source=self.capture(basis,{'main.txt':('100644',b'base')})
        result=self.service.convert(self.member,basis.id,source.id,'変更なし')
        self.assertEqual(result.state,'NO_CHANGE');self.assertIsNone(result.change_request_id)
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_missing_baseline_file_requires_deletion(self):
        basis=self.start();source=self.capture(basis,{'new.txt':('100644',b'new')})
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,source.id,'削除証拠なし')
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_explicit_deletion(self):
        basis=self.start();source=self.capture(basis,{'new.txt':('100644',b'new')})
        candidates=self.service.deletion_candidates(self.member,basis.id)
        self.assertEqual(len(candidates),1)
        evidence=self.service.confirm_deletion(self.member,basis.id,source.id,next(iter(candidates)),str(uuid4()))
        result=self.service.convert(self.member,basis.id,source.id,'移動',deletion_ids=[evidence.id])
        entries=self.git.entries(result.generated_commit)
        self.assertNotIn(self.prefix+'main.txt',entries);self.assertIn(self.prefix+'.env',entries)

    def test_deletion_contradicts_capture(self):
        basis=self.start();source=self.capture(basis)
        candidate=next(iter(self.service.deletion_candidates(self.member,basis.id)))
        evidence=self.service.confirm_deletion(self.member,basis.id,source.id,candidate,str(uuid4()))
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,source.id,'矛盾',deletion_ids=[evidence.id])

    def test_unbound_collection_not_accepted(self):
        basis=self.start();source=self.capture(basis,bind=False)
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,source.id,'後付け不可')

    def test_duplicate_and_changed_input(self):
        basis=self.start();source=self.capture(basis)
        first=self.service.convert(self.member,basis.id,source.id,'同じ変更')
        again=self.service.convert(self.member,basis.id,source.id,'同じ変更')
        self.assertEqual(first.change_request_id,again.change_request_id)
        self.assertEqual(self.db.query(models.ChangeRequest).count(),1)
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,source.id,'違うタイトル')

    def test_main_advance_does_not_change_base(self):
        basis=self.start();source=self.capture(basis)
        tree=self.git.tree({'other.txt':('100644',b'new main')})
        advanced=self.git.run('commit-tree',tree,'-p',self.base,data=b'Other work\n').decode().strip()
        self.git.run('update-ref','refs/heads/main',advanced)
        result=self.service.convert(self.member,basis.id,source.id,'旧基準')
        self.assertEqual(self.db.get(models.ChangeRequest,result.change_request_id).base_commit,self.base)
        self.assertEqual(self.git.read_ref(('heads','main')),advanced)

    def test_db_failure_explicit_recovery_without_collection(self):
        basis=self.start();source=self.capture(basis);original=self.db.commit;calls=[]
        def commit():
            calls.append(1)
            if len(calls)==2:raise RuntimeError('synthetic commit failure')
            return original()
        with patch.object(self.db,'commit',side_effect=commit):
            with self.assertRaises(RuntimeError):self.service.convert(self.member,basis.id,source.id,'復旧')
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,source.id,'復旧')
        result=self.service.convert(self.member,basis.id,source.id,'復旧',recover=True)
        self.assertEqual(result.state,'READY');self.assertEqual(self.db.query(models.ChangeRequest).count(),1)

    def test_deletion_cannot_transfer_to_another_capture(self):
        basis=self.start();source=self.capture(basis,{'new.txt':('100644',b'new')})
        candidate=next(iter(self.service.deletion_candidates(self.member,basis.id)))
        evidence=self.service.confirm_deletion(self.member,basis.id,source.id,candidate,str(uuid4()))
        other=self.capture(basis,{'new.txt':('100644',b'new')})
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,other.id,'転用',deletion_ids=[evidence.id])

    def test_arbitrary_deletion_path_not_a_candidate(self):
        basis=self.start();source=self.capture(basis)
        with self.assertRaises(StorageConflict):self.service.confirm_deletion(self.member,basis.id,source.id,'outside.sh',str(uuid4()))

    def test_capture_generation_tampering_rejected(self):
        basis=self.start();source=self.capture(basis)
        self.db.get(models.WorkCaptureBinding,source.id).source_generation=99;self.db.commit()
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,source.id,'世代不一致')

    def test_membership_revocation_rejects(self):
        basis=self.start();self.db.query(models.TeamMember).filter_by(team_id=self.team.id,user_id=self.member.id).delete();self.db.commit()
        with self.assertRaises(Exception):self.service.deletion_candidates(self.member,basis.id)

    def test_source_journal_corruption_rejected(self):
        basis=self.start();source=self.capture(basis)
        (self.git.journal/(source.id+'.complete')).write_bytes(b'{')
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,source.id,'破損')

    def test_read_failure_not_deletion(self):
        basis=self.start();source=self.capture(basis)
        self.git.run('update-ref','-d',source.git_ref)
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,source.id,'読取不能')
        self.assertEqual(self.db.query(models.ChangeRequest).count(),0)

    def test_previous_request_link_preserves_decision(self):
        basis=self.start();source=self.capture(basis)
        first=self.service.convert(self.member,basis.id,source.id,'初回')
        old=self.db.get(models.ChangeRequest,first.change_request_id)
        self.db.add(models.ChangeRequestReview(change_request_id=old.id,decision='CHANGES_REQUESTED',reviewer_id=self.owner.id,reviewed_at=datetime.utcnow(),reason='修正してください'))
        self.db.commit()
        other=self.capture(basis,{'main.txt':('100644',b'revised')})
        result=self.service.convert(self.member,basis.id,other.id,'修正',previous_request_id=old.id)
        self.assertEqual(result.previous_request_id,old.id)
        self.assertEqual(self.db.get(models.ChangeRequestReview,old.id).decision,'CHANGES_REQUESTED')

    def test_previous_request_other_member_rejected(self):
        basis=self.start();source=self.capture(basis)
        first=self.service.convert(self.member,basis.id,source.id,'初回')
        old=self.db.get(models.ChangeRequest,first.change_request_id);old.submitted_by=self.owner.id;self.db.commit()
        other=self.capture(basis)
        with self.assertRaises(StorageConflict):self.service.convert(self.member,basis.id,other.id,'不正関連',previous_request_id=old.id)

    def test_parallel_conversion_registers_once(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        basis=self.start();source=self.capture(basis);bid=basis.id;sid=source.id;uid=self.member.id
        # 独立Session同士で競合させるため、一時SQLiteへ合成DBだけを複製する。
        path=Path(self.temp.name)/'parallel.sqlite'
        raw=self.engine.raw_connection();destination=sqlite3.connect(path)
        try:raw.driver_connection.backup(destination)
        finally:destination.close();raw.close()
        engine=create_engine('sqlite:///'+path.as_posix());self.addCleanup(engine.dispose)
        results=[];errors=[]
        def worker():
            with sessionmaker(bind=engine)() as db:
                actor=db.get(models.User,uid)
                service=TaskReviewService(db,workspaces=self.workspaces,session_factory=self.git.session)
                try:results.append(service.convert(actor,bid,sid,'並行').change_request_id)
                except Exception as error:errors.append(type(error).__name__)
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(errors,[]);self.assertEqual(len(results),2);self.assertEqual(results[0],results[1])

    def test_empty_initial_main_basis_matches_existing_integration(self):
        from app.integration_git import verify_initial_base
        self.git.run('update-ref','-d','refs/heads/main')
        basis=self.start();verify_initial_base(self.git,basis.base_commit)
        self.assertEqual(self.git.entries(basis.base_commit),{})

    def test_existing_review_decision_accepts_generated_snapshot(self):
        from app.change_request_reviews import decide_change_request
        basis=self.start();source=self.capture(basis)
        result=self.service.convert(self.member,basis.id,source.id,'既存承認')
        with patch('app.change_request_reviews.ReviewGit',return_value=self.git):
            row=decide_change_request(self.db,None,self.team.id,self.project.id,result.change_request_id,self.owner.id,'APPROVED')
        self.assertEqual(row.review.decision,'APPROVED')

    def test_existing_integration_rejects_advanced_main(self):
        from app.change_request_integration import integrate_change_request
        basis=self.start();source=self.capture(basis)
        result=self.service.convert(self.member,basis.id,source.id,'基準不一致')
        self.db.add(models.ChangeRequestReview(change_request_id=result.change_request_id,decision='APPROVED',reviewer_id=self.owner.id,reviewed_at=datetime.utcnow()))
        self.db.commit()
        tree=self.git.tree({'other':('100644',b'other')})
        advanced=self.git.run('commit-tree',tree,'-p',self.base,data=b'Advanced\n').decode().strip()
        self.git.run('update-ref','refs/heads/main',advanced)
        @contextmanager
        def directory(*args):yield None
        @contextmanager
        def main_lock(*args):
            yield lambda *args:self.fail('Main must not be updated')
        with patch('app.change_request_integration.ReviewGit',return_value=self.git),patch.object(self.git,'project',None,create=True),patch.object(self.git,'new_directory',directory),patch('app.change_request_integration.IntegrationJournal',return_value=type('Journal',(),{'read':lambda s:None})()),patch('app.change_request_integration.locked_main',main_lock):
            with self.assertRaises(StorageConflict):integrate_change_request(self.db,None,self.team.id,self.project.id,result.change_request_id,self.member.id)
        self.assertEqual(self.git.read_ref(('heads','main')),advanced)

    def test_unrepresentable_empty_tree_rejected_without_loss(self):
        empty=self.git.run('mktree','-z').decode().strip()
        root=self.git.run('ls-tree','-z',self.base)
        tree=self.git.run('mktree','-z',data=root+('040000 tree '+empty+'\tempty\0').encode()).decode().strip()
        commit=self.git.run('commit-tree',tree,data=b'Empty directory fixture\n').decode().strip()
        self.git.run('update-ref','refs/heads/main',commit)
        with self.assertRaises(StorageConflict):self.start()
        self.assertEqual(self.git.read_ref(('heads','main')),commit)


class MigrationTests(unittest.TestCase):
    def test_explicit_ddl_matches_columns_and_no_task_cascade(self):
        import test_projects
        from app.db import database
        from sqlalchemy import inspect
        engine=test_projects.test_engine();self.addCleanup(test_projects.close_test_engine,engine)
        database.create_startup_tables(engine)
        tables=['work_bases','work_capture_bindings','task_deletion_intents','task_review_conversions']
        self.assertTrue(all(not inspect(engine).has_table(t) for t in tables))
        raw=engine.raw_connection()
        try:
            for name in ('005_task_submission_targets.sql','006_task_submissions.sql','007_task_review_connection.sql'):
                raw.driver_connection.executescript((Path(__file__).parents[1]/'migrations'/name).read_text(encoding='utf-8'))
            for table in tables:
                self.assertEqual({c['name'] for c in inspect(engine).get_columns(table)},set(models.Base.metadata.tables[table].columns.keys()))
                self.assertFalse(any(f['referred_table']=='tasks' for f in inspect(engine).get_foreign_keys(table)))
        finally:raw.close()


class MergeRulesTests(unittest.TestCase):
    def test_outside_and_excluded_preserved(self):
        base={'outside':('100755',b'x'),'task/.env':('100644',b'keep'),'task/a':('100755',b'old')}
        result=merge_task(base,{'a':('100644',b'new')},'task',[])
        self.assertEqual(result['outside'],base['outside']);self.assertEqual(result['task/.env'],base['task/.env'])
        self.assertEqual(result['task/a'][0],'100755')
    def test_file_directory_collision(self):
        with self.assertRaises(StorageConflict):merge_task({'task/a':('100644',b'x')},{'a/b':('100644',b'y')},'task',[])
    def test_explicit_file_directory_replacement(self):
        result=merge_task({'task/a':('100644',b'x')},{'a/b':('100644',b'y')},'task',['task/a'])
        self.assertEqual(set(result),{'task/a/b'})
    def test_boundary_deletion_rejected(self):
        with self.assertRaises(StorageConflict):merge_task({'outside':('100644',b'x')},{'a':('100644',b'y')},'task',['outside'])
    def test_total_limit(self):
        from app.review_git import ReviewLimit
        with patch('app.task_review.MAX_TOTAL_BYTES',1):
            with self.assertRaises(ReviewLimit):merge_task({}, {'a':('100644',b'long')},'task',[])
