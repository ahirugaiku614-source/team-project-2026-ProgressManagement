"""安全な収集結果から既存レビュー依頼へ接続する内部サービス。公開APIは追加しない。"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from app import models, schemas
from app.submissions import lock_target
from app.submission_provider import SubmissionCollection
from app.submission_git import SubmissionReviewGit, GitSnapshotStore, exact
from app.submission_collection import excluded
from app.review_git import MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, ReviewLimit
from app.change_request_reviews import verified_object, verify_snapshot
from app.change_requests import reference_key, row_manifest
from app.project_storage import StorageConflict

POLICY_VERSION = 1


class TaskReviewError(StorageConflict):
    """公開APIで説明可能な固定コード。本文や内部例外は含めない。"""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def conflict():
    raise StorageConflict('Task review evidence is missing, inconsistent or requires review')


def digest(value):
    # 内容本文ではなく変換入力の同一性に使用し、値は通知・ログへ出さない。
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def binding_version(db,task_id):
    row=db.get(models.TaskSubmissionTarget,task_id,populate_existing=True)
    if row is None:conflict()
    return digest([row.task_id,row.project_id,row.student_id,row.folder_path,row.registered_by,row.created_at.isoformat()])


def eligible(path):
    return not any(excluded(part) for part in path.split('/'))


def checked_files(git,commit):
    # 既存レビューと同じモード・全体上限を検査し、commit/tree/blobの破損も拒否する。
    content=verified_object(git,'commit',commit)
    trees=[line[5:].decode() for line in content.split(b'\n\n',1)[0].splitlines() if line.startswith(b'tree ')]
    if len(trees)!=1:conflict()
    verified_object(git,'tree',trees[0])
    files={};total=0
    for path,(mode,oid) in git.entries(commit).items():
        if mode not in ('100644','100755'):conflict()
        body=verified_object(git,'blob',oid)
        if len(body)>MAX_FILE_BYTES:raise ReviewLimit('Project file limit exceeded')
        total+=len(body)
        if total>MAX_TOTAL_BYTES or len(files)>=MAX_FILES:raise ReviewLimit('Project limit exceeded')
        files[path]=(mode,body)
    # 途中のtreeも既存レビュー検証と同じく内容ハッシュを確認する。
    for entry in git.run('ls-tree','-r','-t','-z','--full-tree',commit).split(b'\0'):
        if not entry:continue
        meta,_=entry.split(b'\t',1);_,kind,oid=meta.decode().split()
        if kind=='tree':verified_object(git,'tree',oid)
    # 平坦化で表現できない空tree等を黙って落とさない。対象外treeの損失は拒否する。
    if git.tree(files)!=trees[0]:raise TaskReviewError('UNSUPPORTED_EMPTY_TREE')
    return files


class TaskReviewGit(SubmissionReviewGit):
    def basis_ref(self,basis_id):
        loose=self.read_ref(('work-bases',basis_id))
        packed=self.packed_refs().get(('refs/work-bases/'+basis_id).encode())
        if packed is not None and loose!=packed:conflict()
        return loose

    def pin_basis(self,basis_id,commit):
        self.commit_exists(commit)
        with self.git_lock(self.shared,'packed-refs.lock'), self.new_directory(self.shared,'refs') as refs:
            with self.new_directory(refs,'work-bases') as bases:
                with self.git_lock(bases,basis_id+'.lock'):
                    if self.basis_ref(basis_id) is not None:conflict()
                    self.write_once(bases,basis_id,(commit+'\n').encode())


@dataclass(frozen=True)
class WorkspaceInitialization:
    basis_id: str
    target: object
    base_commit: str
    workspace_id: str
    source_generation: int


class DeniedWorkspaces:
    """実workspaceの初期化・検証は未接続。OIDの後付けや既存領域上書きは行わない。"""
    def initialize(self,*args):
        from app.project_storage import StorageUnavailable
        raise StorageUnavailable('Workspace verifier is not connected')
    verify = initialize
    verify_collection = initialize


def merge_task(base,captured,folder,deletions):
    """欠落を削除と推測しない。置換は明示削除後の衝突検査で安全な場合だけ。"""
    if not captured:raise TaskReviewError('EMPTY_CAPTURE_UNSUPPORTED')
    prefix=folder+'/'
    if folder in base:conflict()  # 登録領域の入口が普通ファイルならディレクトリ扱いしない。
    result=dict(base)
    affected={path for path in base if path.startswith(prefix) and eligible(path[len(prefix):])}
    translated={prefix+path:(base.get(prefix+path,('100644',b''))[0],body)
                for path,(_,body) in captured.items()}
    if set(deletions)&set(translated):conflict()
    if affected-set(translated)-set(deletions):conflict()
    if set(deletions)-affected:conflict()
    for path in deletions:result.pop(path)
    result.update(translated)
    for path in result:
        parts=path.split('/')
        if any('/'.join(parts[:i]) in result for i in range(1,len(parts))):conflict()
    if len(result)>MAX_FILES or sum(len(body) for _,body in result.values())>MAX_TOTAL_BYTES:raise ReviewLimit('Project limit exceeded')
    return result


class TaskReviewService:
    def __init__(self,db,storage=None,*,workspaces=None,session_factory=None):
        self.db=db
        self.workspaces=workspaces if workspaces is not None else DeniedWorkspaces()
        self.session_factory=session_factory or (lambda p:TaskReviewGit(storage).session(p))

    def start(self,actor,team_id,project_id,task_id):
        with self.session_factory(project_id) as git:
            target=lock_target(self.db,actor,team_id,project_id,task_id)
            if self.db.get(models.Task,task_id).status == models.TaskStatus.DONE:
                raise TaskReviewError('TASK_FINISHED')
            base=git.main_commit();checked_files(git,base)
            basis_id=str(uuid4())
            proof=self.workspaces.initialize(target,base,basis_id)
            if (not isinstance(proof,WorkspaceInitialization) or proof.basis_id!=basis_id
                    or proof.target!=target or proof.base_commit!=base
                    or str(UUID(proof.workspace_id))!=proof.workspace_id
                    or type(proof.source_generation) is not int or proof.source_generation<=0
                    or self.workspaces.verify(proof) is not True):conflict()
            row=models.WorkBasis(id=basis_id,member_id=actor.id,team_id=team_id,project_id=project_id,
                task_id=task_id,workspace_id=proof.workspace_id,source_generation=proof.source_generation,
                folder_path=target.folder_path,binding_version=binding_version(self.db,task_id),base_commit=base,
                policy_version=POLICY_VERSION,created_at=datetime.utcnow(),state='ACTIVE')
            # 新規初期化の証拠からだけ保存元世代を登録する。利用者は世代を指定できない。
            if self.db.get(models.WorkWorkspaceRevision,proof.workspace_id) is not None:conflict()
            self.db.add(models.WorkWorkspaceRevision(workspace_id=proof.workspace_id,generation=proof.source_generation))
            git.pin_basis(basis_id,base)
            git.journal_create(basis_id,'basis',self._basis_record(row))
            self.db.add(row);self.db.commit()
            return row

    def _basis_record(self,basis):
        return {key:getattr(basis,key) for key in ('id','member_id','team_id','project_id','task_id',
            'workspace_id','source_generation','folder_path','binding_version','base_commit','policy_version')}

    def _basis(self,git,actor,basis_id,active=True):
        basis=self.db.get(models.WorkBasis,basis_id,populate_existing=True)
        if basis is None or basis.member_id!=actor.id:conflict()
        try:target=lock_target(self.db,actor,basis.team_id,basis.project_id,basis.task_id)
        except Exception:
            basis=self.db.get(models.WorkBasis,basis_id);basis.state='REVOKED';self.db.commit();raise
        if self.db.get(models.Task,basis.task_id).status == models.TaskStatus.DONE:
            basis.state='REVOKED';self.db.commit();raise TaskReviewError('TASK_FINISHED')
        if (target.folder_path!=basis.folder_path or binding_version(self.db,basis.task_id)!=basis.binding_version):
            basis.state='REVOKED';self.db.commit();conflict()
        if active and basis.state!='ACTIVE':raise TaskReviewError('WORK_BASIS_INACTIVE')
        revision=self.db.get(models.WorkWorkspaceRevision,basis.workspace_id,populate_existing=True)
        if revision is None or revision.generation!=basis.source_generation:
            basis.state='REVOKED';self.db.commit();raise TaskReviewError('WORK_BASIS_REVOKED')
        if basis.policy_version!=POLICY_VERSION or git.basis_ref(basis.id)!=basis.base_commit:conflict()
        if not exact(git.journal_read(basis.id,'basis'),self._basis_record(basis)):conflict()
        proof=WorkspaceInitialization(basis.id,target,basis.base_commit,basis.workspace_id,basis.source_generation)
        if self.workspaces.verify(proof) is not True:conflict()
        checked_files(git,basis.base_commit)
        return basis,target

    def end(self,actor,basis_id,state='ENDED'):
        # ログアウト処理とは接続しない。明示終了/更新でもGit参照や作業内容を消さない。
        if state not in ('ENDED','SUPERSEDED'):conflict()
        basis=self.db.get(models.WorkBasis,basis_id)
        if basis is None or basis.member_id!=actor.id:conflict()
        with self.session_factory(basis.project_id) as git:
            basis,_=self._basis(git,actor,basis_id);basis.state=state;self.db.commit()

    def deletion_candidates(self,actor,basis_id):
        basis=self.db.get(models.WorkBasis,basis_id)
        if basis is None:conflict()
        with self.session_factory(basis.project_id) as git:
            basis,_=self._basis(git,actor,basis_id)
            return self._candidates(git,basis)

    def _candidates(self,git,basis):
        prefix=basis.folder_path+'/'
        return {str(uuid5(NAMESPACE_URL,'task-delete:'+basis.id+':'+path+':'+oid)):
                {'path':path,'blob':oid,'mode':mode}
                for path,(mode,oid) in git.entries(basis.base_commit).items()
                if path.startswith(prefix) and eligible(path[len(prefix):])}

    def confirm_deletion(self,actor,basis_id,submission_id,candidate_id,confirmation_id):
        if str(UUID(confirmation_id))!=confirmation_id:conflict()
        basis=self.db.get(models.WorkBasis,basis_id)
        if basis is None:conflict()
        with self.session_factory(basis.project_id) as git:
            basis,target=self._basis(git,actor,basis_id)
            self._submission(git,actor,basis,target,submission_id)
            candidate=self._candidates(git,basis).get(candidate_id)
            if candidate is None:conflict()
            existing=self.db.query(models.TaskDeletionIntent).filter_by(basis_id=basis.id,submission_id=submission_id,path=candidate['path']).first()
            if existing is not None:
                # 応答喪失後の同じ確認操作は同じ証拠を返す。異なる確認で上書きしない。
                if (existing.confirmation_id,existing.blob,existing.mode)!=(confirmation_id,candidate['blob'],candidate['mode']):conflict()
                return existing
            row=models.TaskDeletionIntent(id=str(uuid4()),basis_id=basis.id,submission_id=submission_id,
                member_id=actor.id,project_id=basis.project_id,task_id=basis.task_id,
                binding_version=basis.binding_version,policy_version=basis.policy_version,
                confirmation_id=confirmation_id,**candidate)
            self.db.add(row);self.db.commit();return row

    def _submission(self,git,actor,basis,target,sid):
        row=self.db.get(models.TaskSubmission,sid,populate_existing=True)
        if (row is None or row.state!='SUBMITTED' or row.student_id!=actor.id
                or (row.team_id,row.project_id,row.task_id,row.folder_path)!=
                (basis.team_id,basis.project_id,basis.task_id,basis.folder_path)):conflict()
        binding=self.db.get(models.WorkCaptureBinding,sid)
        if (binding is None or (binding.basis_id,binding.workspace_id,binding.source_generation,binding.editing_session_id)!=
                (basis.id,basis.workspace_id,basis.source_generation,row.editing_session_id)):conflict()
        permit=self.db.get(models.SubmissionPermit,row.permit_id,populate_existing=True)
        if (permit is None or permit.consumed_at is None or permit.token_digest!=row.permit_digest
                or (permit.student_id,permit.team_id,permit.project_id,permit.task_id,permit.folder_path,permit.editing_session_id)!=
                (row.student_id,row.team_id,row.project_id,row.task_id,row.folder_path,row.editing_session_id)):conflict()
        request=SubmissionCollection(row.id,row.request_id,row.editing_session_id,target)
        receipt=GitSnapshotStore(None)._verify(git,request)
        if (row.git_ref,row.commit_id,row.file_count,row.total_bytes)!=(receipt.git_ref,receipt.commit_id,receipt.file_count,receipt.total_bytes):conflict()
        return row

    def bind_collection(self,actor,basis_id,submission_id):
        """認可済み受付後・収集前にだけ呼ぶ。実session検証器未接続では拒否する。"""
        basis=self.db.get(models.WorkBasis,basis_id)
        if basis is None:conflict()
        with self.session_factory(basis.project_id) as git:
            basis,target=self._basis(git,actor,basis_id)
            row=self.db.get(models.TaskSubmission,submission_id,populate_existing=True)
            if (row is None or row.state!='PROCESSING' or row.student_id!=actor.id
                    or (row.team_id,row.project_id,row.task_id,row.folder_path)!=
                    (basis.team_id,basis.project_id,basis.task_id,basis.folder_path)
                    or self.workspaces.verify_collection(basis,row) is not True):conflict()
            permit=self.db.get(models.SubmissionPermit,row.permit_id)
            if permit is None or permit.consumed_at is None or permit.token_digest!=row.permit_digest:conflict()
            if self.db.get(models.WorkCaptureBinding,submission_id) is not None:conflict()
            self.db.add(models.WorkCaptureBinding(submission_id=row.id,basis_id=basis.id,
                workspace_id=basis.workspace_id,source_generation=basis.source_generation,
                editing_session_id=row.editing_session_id))
            self.db.commit()

    def validate_collection_binding(self,actor,basis_id,submission_id):
        """収集完了の公開前にも基準を再確認する。元データは再収集しない。"""
        basis=self.db.get(models.WorkBasis,basis_id)
        if basis is None:conflict()
        with self.session_factory(basis.project_id) as git:
            basis,_=self._basis(git,actor,basis_id)
            row=self.db.get(models.TaskSubmission,submission_id,populate_existing=True)
            bound=self.db.get(models.WorkCaptureBinding,submission_id,populate_existing=True)
            if (row is None or row.state!='PROCESSING' or bound is None
                    or (bound.basis_id,bound.workspace_id,bound.source_generation,bound.editing_session_id)!=
                    (basis.id,basis.workspace_id,basis.source_generation,row.editing_session_id)
                    or self.workspaces.verify_collection(basis,row) is not True):conflict()

    def _plan(self,git,actor,basis,source,title,description,deletion_ids,previous_request_id):
        """確認と提出で同じ保存証拠を検証する。本文は返さず入力を固定する。"""
        request_key=str(uuid5(NAMESPACE_URL,'task-review-v1:'+source.id))
        payload=schemas.ChangeRequestCreate(request_key=request_key,title=title,description=description)
        previous=self.db.get(models.ChangeRequest,previous_request_id) if previous_request_id is not None else None
        if previous_request_id is not None and (previous is None or previous.project_id!=basis.project_id or previous.submitted_by!=actor.id):conflict()
        if len(set(deletion_ids))!=len(deletion_ids):conflict()
        deletes=[];catalog=self._candidates(git,basis)
        for did in sorted(deletion_ids):
            d=self.db.get(models.TaskDeletionIntent,did)
            if (d is None or (d.basis_id,d.submission_id,d.member_id,d.project_id,d.task_id,d.binding_version,d.policy_version)!=
                    (basis.id,source.id,actor.id,basis.project_id,basis.task_id,basis.binding_version,basis.policy_version)
                    or {'path':d.path,'blob':d.blob,'mode':d.mode} not in catalog.values()):conflict()
            deletes.append({'id':d.id,'path':d.path,'blob':d.blob,'mode':d.mode,'confirmation_id':d.confirmation_id})
        recipe={'version':1,'basis':self._basis_record(basis),'submission_id':source.id,'source_commit':source.commit_id,
                'request_key':request_key,'title':payload.title,'description':payload.description,
                'deletions':deletes,'previous_request_id':previous_request_id}
        return recipe

    def preview(self,actor,basis_id,submission_id,title,description=None,*,deletion_ids=(),previous_request_id=None):
        """確認操作は変換commit/参照/ChangeRequestを作成しない。本文は返さない。"""
        basis=self.db.get(models.WorkBasis,basis_id)
        if basis is None:conflict()
        with self.session_factory(basis.project_id) as git:
            basis,target=self._basis(git,actor,basis_id)
            source=self._submission(git,actor,basis,target,submission_id)
            recipe=self._plan(git,actor,basis,source,title,description,deletion_ids,previous_request_id)
            base=checked_files(git,basis.base_commit);captured=checked_files(git,source.commit_id)
            files=merge_task(base,captured,basis.folder_path,[d['path'] for d in recipe['deletions']])
            changes=[{'path':p,'change':'ADDED' if p not in base else 'DELETED' if p not in files else 'MODIFIED'}
                     for p in sorted(set(base)|set(files)) if base.get(p)!=files.get(p)]
            # 一時的な確認IDはDBへ固定する。後の提出は同じ入力ダイジェストを要求する。
            row=models.WorkReviewPreview(id=str(uuid4()),basis_id=basis.id,submission_id=source.id,
                input_digest=digest(recipe),expires_at=datetime.utcnow()+timedelta(minutes=10))
            self.db.add(row);self.db.commit()
            return {'confirmation_id':row.id,'expires_at':row.expires_at,'changes':changes,
                    'no_change':not changes,'excluded_policy_version':POLICY_VERSION,
                    'excluded_paths':[p for p in sorted(base) if p.startswith(basis.folder_path+'/') and not eligible(p[len(basis.folder_path)+1:])],
                    'main_differs':git.read_ref(('heads','main')) not in (None,basis.base_commit)}

    def convert(self,actor,basis_id,submission_id,title,description=None,*,deletion_ids=(),previous_request_id=None,recover=False,confirmation_id=None):
        basis=self.db.get(models.WorkBasis,basis_id)
        if basis is None:conflict()
        with self.session_factory(basis.project_id) as git:
            basis,target=self._basis(git,actor,basis_id)
            source=self._submission(git,actor,basis,target,submission_id)
            recipe=self._plan(git,actor,basis,source,title,description,deletion_ids,previous_request_id)
            request_key=recipe['request_key'];deletes=recipe['deletions']
            fingerprint=digest(recipe)
            if confirmation_id is not None:
                confirmed=self.db.get(models.WorkReviewPreview,confirmation_id,populate_existing=True)
                if (confirmed is None or (confirmed.basis_id,confirmed.submission_id,confirmed.input_digest)!=
                        (basis.id,source.id,fingerprint)):
                    raise TaskReviewError('PREVIEW_CHANGED')
                # 確定済みの同一入力の応答確認では、確認期限切れでも再登録せず既存証拠を照合する。
                existing=self.db.get(models.TaskReviewConversion,source.id)
                if confirmed.expires_at<=datetime.utcnow() and existing is None:
                    raise TaskReviewError('PREVIEW_EXPIRED')
            conversion=self.db.get(models.TaskReviewConversion,source.id,populate_existing=True)
            if conversion is not None:
                if conversion.input_digest!=fingerprint or not exact(json.loads(conversion.recipe),recipe):conflict()
                saved=git.journal_read(source.id,'conversion')
                if saved is None or not exact(saved['recipe'],recipe):conflict()
                if conversion.state in ('READY','NO_CHANGE'):
                    return self._finish(git,conversion,saved,recipe,actor,commit=False)
                if not recover:conflict()
                return self._finish(git,conversion,saved,recipe,actor)
            if git.journal_read(source.id,'conversion') is not None:conflict()
            conversion=models.TaskReviewConversion(submission_id=source.id,basis_id=basis.id,input_digest=fingerprint,
                recipe=json.dumps(recipe,sort_keys=True),state='PREPARED',previous_request_id=previous_request_id)
            self.db.add(conversion);self.db.commit()  # 入力をGit変換より先に固定する。
            try:
                base=checked_files(git,basis.base_commit)
                captured=checked_files(git,source.commit_id)
                files=merge_task(base,captured,basis.folder_path,[d['path'] for d in deletes])
                tree=git.tree(files)
                unchanged=tree==git.run('rev-parse',basis.base_commit+'^{tree}').decode().strip()
                generated=None if unchanged else git.run('commit-tree',tree,'-p',basis.base_commit,
                    data=('Review snapshot '+reference_key(basis.project_id,actor.id,request_key)+'\n').encode()).decode().strip()
                record={'recipe':recipe,'generated_commit':generated,'created_at':datetime.utcnow().isoformat()}
                git.journal_create(source.id,'conversion',record)
                return self._finish(git,conversion,record,recipe,actor)
            except Exception:
                self.db.rollback();conversion=self.db.get(models.TaskReviewConversion,submission_id)
                conversion.state='REVIEW_REQUIRED';self.db.commit();raise

    def _finish(self,git,conversion,saved,recipe,actor,commit=True):
        # PREPAREDのcommitで認可ロックは解放されるため、DB確定の直前にも再取得する。
        # 更新側のトリガーと同じ基準行をロックし、途中の失効を成功で上書きしない。
        self._basis(git,actor,recipe['basis']['id'])
        basis=recipe['basis'];base=basis['base_commit'];head=saved['generated_commit']
        if conversion.generated_commit not in (None,head):conflict()
        if not commit:
            if head is None and (conversion.state!='NO_CHANGE' or conversion.change_request_id is not None):conflict()
            if head is not None and (conversion.state!='READY' or conversion.generated_commit!=head or conversion.change_request_id is None):conflict()
        files=merge_task(checked_files(git,base),checked_files(git,recipe['source_commit']),
                         basis['folder_path'],[d['path'] for d in recipe['deletions']])
        expected_tree=git.tree(files)
        if head is None:
            # 変更なしでも入力から再構成して照合する。収集元へはアクセスしない。
            if expected_tree!=git.run('rev-parse',base+'^{tree}').decode().strip():conflict()
            conversion.state='NO_CHANGE'
            if commit:self.db.commit()
            return conversion
        checked_files(git,head)
        if expected_tree!=git.run('rev-parse',head+'^{tree}').decode().strip():conflict()
        # 既存の登録処理へ既知のbase/headを渡す。新しいworkspace収集やmain再取得はしない。
        from app.change_requests import register_review_snapshot
        manifest={'version':1,'ref_key':reference_key(basis['project_id'],actor.id,recipe['request_key']),
            'project_id':basis['project_id'],'submitted_by':actor.id,'request_key':recipe['request_key'],
            'title':recipe['title'],'description':recipe['description'],'created_at':saved['created_at'],
            'base_commit':base,'snapshot_commit':head}
        row=register_review_snapshot(self.db,git,manifest,repair=commit)
        if conversion.change_request_id not in (None,row.id):conflict()
        if conversion.generated_commit not in (None,head):conflict()
        conversion.generated_commit=head;conversion.change_request_id=row.id;conversion.state='READY'
        if commit:self.db.commit()
        return conversion
