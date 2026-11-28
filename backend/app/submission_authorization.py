"""提出認可証拠。RPCサービス認証とは別鍵・別用途で発行/照合する。"""
from dataclasses import asdict, dataclass
import hashlib
import hmac
import time

from app.submission_rpc import canonical, reject, target_from, validate_request


class DeniedSessionVerifier:
    """実code-server未接続の既定実装。IDやクライアントのdirty申告だけでは許可しない。"""
    def verify(self, target, session_id, generation):
        reject()


@dataclass(frozen=True)
class VerifiedSession:
    target: object
    session_id: str
    source_generation: int
    expires_at: int
    saved: bool


@dataclass(frozen=True)
class SnapshotSession:
    """方式Cの認証済み編集セッション。dirtyの真偽を表す値を持たない。"""
    target: object
    session_id: str
    source_generation: int
    expires_at: int
    basis_id: str
    workspace_id: str


def check_session(verifier, target, session, generation, now):
    # 検証器は認証済みsessionの現状態を独立確認する。本文や強制保存は扱わない。
    evidence = verifier.verify(target, session, generation)
    if (type(evidence) not in (VerifiedSession, SnapshotSession) or evidence.target != target
            or evidence.session_id != session or type(evidence.source_generation) is not int
            or evidence.source_generation != generation
            or (type(evidence) is VerifiedSession and evidence.saved is not True)
            or type(evidence.expires_at) is not int or evidence.expires_at <= now): reject()
    return evidence


def auth_mac(key, claims):
    if not isinstance(key, bytes) or len(key) < 32: reject()
    return hmac.new(key, b'STUDENT_SUBMISSION_AUTH_V1\0' + canonical(claims), hashlib.sha256).hexdigest()


def _authorized_record(db, actor, target, submission_id, request_id, session_id, states):
    """用途間で共有する本人・現在の権限・保存済み対応の検査。

    収集許可の期限や現在のdirty状態はここでは判定しない。それらは新規収集の
    発行側だけで必須にする。照合・復旧も、他人の記録や失効した現在の権限を
    通過させず、既存のlock_targetと同じDBロック順を使う。
    """
    from app import models
    from app.submissions import lock_target
    from app.submission_rpc import uuid_text
    for value in (submission_id, request_id, session_id):
        uuid_text(value)
    current = lock_target(db, actor, target.team_id, target.project_id, target.task_id)
    if current != target:
        reject()
    row = db.get(models.TaskSubmission, submission_id, populate_existing=True)
    if (row is None or row.state not in states
            or (row.student_id, row.team_id, row.project_id, row.task_id,
                row.folder_path, row.request_id, row.editing_session_id) !=
               (target.student_id, target.team_id, target.project_id, target.task_id,
                target.folder_path, request_id, session_id)):
        reject()
    permit = db.get(models.SubmissionPermit, row.permit_id, populate_existing=True)
    if (permit is None or permit.consumed_at is None or permit.purpose != 'TASK_SUBMISSION'
            or permit.token_digest != row.permit_digest
            or (permit.student_id, permit.team_id, permit.project_id, permit.task_id,
                permit.folder_path, permit.editing_session_id) !=
               (row.student_id, row.team_id, row.project_id, row.task_id,
                row.folder_path, row.editing_session_id)):
        reject()
    return row, permit


class BackendAuthorizationIssuer:
    """認証済みUserを持つ管理側だけで使用。収集側にDB接続情報を渡さない。

    actorは既存Firebase依存関数が返したUserを渡す。HTTP入力からUserを生成しない。
    保存済みPROCESSING行と消費済みpermitを確認してから短命証拠を発行する。
    """
    def __init__(self, db, actor, key, collector_id, generation_for, *, sessions=None, clock=lambda:int(time.time())):
        self.db,self.actor,self.key=db,actor,key
        self.collector_id,self.generation_for=collector_id,generation_for
        self.sessions=sessions if sessions is not None else DeniedSessionVerifier()
        self.clock=clock

    def issue(self, request):
        try:return self._issue(request)
        except Exception:reject()

    def _issue(self, request):
        import calendar
        now=self.clock();validate_request(request,now)
        target=target_from(request['target'])
        row, permit = _authorized_record(self.db, self.actor, target,
            request['submission_id'], request['request_id'], request['editing_session_id'],
            states=('PROCESSING',))
        if self.generation_for(target)!=request['source_generation']:reject()
        expires=calendar.timegm(permit.expires_at.utctimetuple())
        session=check_session(self.sessions,target,row.editing_session_id,request['source_generation'],now)
        end=min(request['expires_at'],expires,session.expires_at,now+30)
        if end<=now:reject()
        binding={k:v for k,v in request.items() if k!='authorization'}
        claims={'version':1,'purpose':'COLLECT_FOR_SUBMISSION','audience':self.collector_id,
                'binding':binding,'issued_at':now,'expires_at':end}
        if type(session) is SnapshotSession:
            # dirtyの証明とは別用途で、現在のセッション認可を署名する。
            from app import models
            binding_row=self.db.get(models.WorkCaptureBinding,row.id,populate_existing=True)
            if (binding_row is None or (binding_row.basis_id,binding_row.workspace_id,
                    binding_row.source_generation,binding_row.editing_session_id) !=
                    (session.basis_id,session.workspace_id,session.source_generation,session.session_id)):reject()
            claims.update(version=2,purpose='COLLECT_SAVED_SNAPSHOT',
                          basis_id=session.basis_id,workspace_id=session.workspace_id)
        return {'claims':claims,'signature':auth_mac(self.key,claims)}


class CollectorAuthorizationVerifier:
    """DBなしで署名・固定対象・世代・収集サービス・sessionを照合する。

    認可後のDB失効を収集側だけで即時検出することはできない。証拠は最大30秒で、
    管理側の前後認可と組み合わせる。検証器異常はTrueへ変換しない。
    """
    def __init__(self,key,collector_id,target,generation,*,sessions=None,clock=lambda:int(time.time()),workspace_id=None):
        self.key,self.collector_id,self.target,self.generation=key,collector_id,target,generation
        self.sessions=sessions if sessions is not None else DeniedSessionVerifier();self.clock=clock
        self.workspace_id=workspace_id

    def __call__(self,request):
        try:
            now=self.clock();validate_request(request,now)
            if request['version']!=2:reject()
            evidence=request['authorization']
            if not isinstance(evidence,dict) or set(evidence)!={'claims','signature'}:reject()
            claims=evidence['claims'];signature=evidence['signature']
            if not isinstance(claims,dict):reject()
            expected={'version','purpose','audience','binding','issued_at','expires_at'}
            if claims.get('version')==2:expected|={'basis_id','workspace_id'}
            if set(claims)!=expected:reject()
            if type(signature) is not str or len(signature)!=64 or any(c not in '0123456789abcdef' for c in signature):reject()
            if not hmac.compare_digest(signature,auth_mac(self.key,claims)):reject()
            binding={k:v for k,v in request.items() if k!='authorization'}
            snapshot = type(claims['version']) is int and claims['version']==2 and claims['purpose']=='COLLECT_SAVED_SNAPSHOT'
            legacy = type(claims['version']) is int and claims['version']==1 and claims['purpose']=='COLLECT_FOR_SUBMISSION'
            if (not (snapshot or legacy) or claims['audience']!=self.collector_id
                    or canonical(claims['binding'])!=canonical(binding)
                    or any(type(claims[f]) is not int for f in ('issued_at','expires_at'))
                    or not claims['issued_at']<=now<claims['expires_at']<=claims['issued_at']+30
                    or target_from(request['target'])!=self.target or request['source_generation']!=self.generation):reject()
            if not snapshot:
                check_session(self.sessions,self.target,request['editing_session_id'],self.generation,now)
            else:
                from app.submission_rpc import uuid_text
                uuid_text(claims['basis_id']);uuid_text(claims['workspace_id'])
                if self.workspace_id is None or claims['workspace_id']!=self.workspace_id:reject()
            # 方式Cは管理側のDB認可・セッション検証に基づく最大30秒の署名を使用。
            # dirty=falseを作らず、収集後の管理側認可も引き続き必須とする。
            return True
        except Exception:
            reject()


def authorized_rpc_provider(db, authenticated_user, *, rpc_keys, authorization_key,
                            collector_id, target, generation, store, transport=None,
                            sessions=None, ledger=None, clock=lambda:int(time.time())):
    """管理側の明示接続factory。FastAPIの既定dependencyは変更しない。

    本番では永続ledgerと信頼済みsession検証器を必ず注入する。未設定なら拒否する。
    storeは管理側Git保存処理で、収集サービスには渡らない。
    """
    from uuid import uuid4
    from app.submission_rpc import CollectorRPCClient
    from app.submission_git import ManagedSnapshotProvider
    if sessions is None or ledger is None or store is None or type(generation) is not int or generation<=0:reject()
    if authorization_key in (rpc_keys.request_key,rpc_keys.response_key):reject()
    def generation_for(current):
        if current!=target:reject()
        return generation
    issuer=BackendAuthorizationIssuer(db,authenticated_user,authorization_key,collector_id,
                                      generation_for,sessions=sessions,clock=clock)
    client=CollectorRPCClient(rpc_keys,ledger,generation_for,transport,clock=clock,authorization_issuer=issuer)
    def current_authorization(request):
        # 発行側と同じ検査を再利用し、前後確認に別の認可ルールを作らない。
        # 証拠の発行はDB書込みや許可の再消費を行わない。
        now=clock()
        payload={'version':2,'operation':'COLLECT_SAVED_TASK','submission_id':request.submission_id,
            'request_id':request.request_id,'editing_session_id':request.editing_session_id,
            'target':asdict(request.target),'source_generation':generation_for(request.target),
            'nonce':str(uuid4()),'issued_at':now,'expires_at':now+90,
            'client_service':'submission-manager','server_service':'task-collector','authorization':None}
        issuer.issue(payload)
        return True

    def evidence_authorization(request):
        # 保存済み証拠の読取り専用認可。署名を発行せず、RPC・台帳消費も呼ばない。
        # PROCESSINGはGit成功後のDB確定前、REVIEW_REQUIREDは明示復旧のために許す。
        # 新規収集を許す状態の追加ではない。既存証拠の完全性はGit storeと呼出し元が
        # 照合し、過去のsession ID・消費済みpermitとの結合は現在も厳密に維持する。
        # 現在のdirty状態やpermit期限を過去のスナップショットへ遡って適用しない。
        from app.submission_targets import TargetForbidden, TargetInvalid, TargetConflict
        from app.submission_provider import ProviderUnavailable
        try:
            if request.target != target:
                reject()
            _authorized_record(db, authenticated_user, request.target,
                request.submission_id, request.request_id, request.editing_session_id,
                states=('PROCESSING', 'SUBMITTED', 'REVIEW_REQUIRED'))
            return True
        except (TargetForbidden, TargetInvalid, TargetConflict):
            # 認可拒否だけを既存provider契約へ変換する。DB停止や内部例外を
            # 証拠破損に変換せず、呼出し側で照合不能として扱わせる。
            raise ProviderUnavailable('Stored submission access denied') from None

    # 収集の保存状態検査は従来どおりissuer内の実検証器を必須にする。
    # 照合と復旧は同じ読取り権限を使うが、それぞれ専用の入口へ明示接続する。
    return ManagedSnapshotProvider(None,store,current_authorization,current_authorization,
        collector=client.collect, verify_authorize=evidence_authorization,
        recover_authorize=evidence_authorization)
