"""初期化専用のHMAC/認可契約。収集RPCとは鍵の用途・フレーム上限を分離する。"""
import base64
from dataclasses import asdict
import hashlib
import hmac
import os
import re
import time
from uuid import uuid4

from app.submission_rpc import pack,unpack,canonical,target_from,uuid_text,reject,RPCKeys
from app.review_git import valid_path
from app.workspace_scope import WorkspaceInitialization
from app.managed_process import ManagedProcess

MAX_FRAME=15*1024*1024
REQUEST_DOMAIN=b'WORKSPACE_REQUEST_V1\0'
RESPONSE_DOMAIN=b'WORKSPACE_RESPONSE_V1\0'


def encode_snapshot(files):
    result={};total=0
    for path,(mode,body) in files.items():
        # Taskフォルダー名の規則とProject内のGitファイル名は別契約。
        # .gitignore等は許可するが、.git・領域外・特殊Gitモードは許可しない。
        valid_path(path)
        if mode not in ('100644','100755') or not isinstance(body,bytes) or len(body)>1024*1024:reject()
        total+=len(body)
        if total>10*1024*1024 or len(result)>=1000:reject()
        result[path]={'mode':mode,'data':base64.b64encode(body).decode('ascii')}
    return result


def decode_snapshot(files):
    if not isinstance(files,dict) or len(files)>1000:reject()
    result={}
    for path,item in files.items():
        if not isinstance(item,dict) or set(item)!={'mode','data'}:reject()
        try:body=base64.b64decode(item['data'],validate=True)
        except Exception:reject()
        result[path]=(item['mode'],body)
    encode_snapshot(result)  # モード・パス・全体上限も同じ固定規則で確認する。
    return result


def fingerprint(request):
    return hashlib.sha256(canonical({k:v for k,v in request.items() if k not in
        ('issued_at','expires_at','nonce','authorization','operation')})).hexdigest()


def validate(request,now):
    if not isinstance(request,dict) or set(request)!={'version','operation','target','basis_id','request_id',
            'base_commit','generation','files','proof','issued_at','expires_at','nonce','authorization'}:reject()
    if type(request['version']) is not int or request['version']!=1 or request['operation'] not in ('INITIALIZE','VERIFY','UNCHANGED','LOOKUP'):reject()
    target_from(request['target'])
    for name in ('basis_id','request_id','nonce'):uuid_text(request[name])
    if (type(request['generation']) is not int or request['generation']!=1
            or not isinstance(request['base_commit'],str) or not re.fullmatch('[0-9a-f]{40}',request['base_commit'])
            or type(request['issued_at']) is not int or type(request['expires_at']) is not int
            or not request['issued_at']<=now<request['expires_at']<=request['issued_at']+20):reject()
    if request['operation']=='INITIALIZE':
        if request['proof'] is not None:reject()
        decode_snapshot(request['files'])
    elif request['files'] is not None:reject()


def authorization_signature(key,request):
    if not isinstance(key,bytes) or len(key)<32:reject()
    return hmac.new(key,b'WORKSPACE_AUTH_V1\0'+canonical({k:v for k,v in request.items() if k!='authorization'}),hashlib.sha256).hexdigest()


class WorkspaceAuthorizationIssuer:
    def __init__(self,db,actor,key):self.db,self.actor,self.key=db,actor,key
    def issue(self,request):
        from app import models
        from app.submissions import lock_target
        target=target_from(request['target'])
        if lock_target(self.db,self.actor,target.team_id,target.project_id,target.task_id)!=target:reject()
        if request['operation'] in ('INITIALIZE','LOOKUP'):
            op=self.db.query(models.WorkEnvironmentOperation).filter_by(member_id=self.actor.id,
                request_id=request['request_id'],team_id=target.team_id,project_id=target.project_id,
                task_id=target.task_id,result_basis_id=request['basis_id']).first()
            if op is None or op.state not in ('PROCESSING','CONFIRMED') or op.base_commit!=request['base_commit']:reject()
            if request['operation']=='INITIALIZE' and op.state!='PROCESSING':reject()
        else:
            basis=self.db.get(models.WorkBasis,request['basis_id'],populate_existing=True)
            proof=request['proof']
            if basis is None and request['operation']=='VERIFY':
                op=self.db.query(models.WorkEnvironmentOperation).filter_by(member_id=self.actor.id,
                    result_basis_id=request['basis_id'],base_commit=request['base_commit'],team_id=target.team_id,
                    project_id=target.project_id,task_id=target.task_id,state='PROCESSING').first()
                if op is None or not isinstance(proof,dict) or proof.get('basis_id')!=request['basis_id'] or proof.get('target')!=asdict(target) or proof.get('base_commit')!=request['base_commit']:reject()
                return authorization_signature(self.key,request)
            if (basis is None or basis.state!='ACTIVE' or basis.member_id!=self.actor.id
                    or basis.base_commit!=request['base_commit'] or basis.source_generation!=request['generation']
                    or (basis.team_id,basis.project_id,basis.task_id,basis.folder_path)!=(target.team_id,target.project_id,target.task_id,target.folder_path)
                    or proof!=asdict(WorkspaceInitialization(basis.id,target,basis.base_commit,basis.workspace_id,basis.source_generation))):reject()
        return authorization_signature(self.key,request)


class WorkspaceAuthorizationVerifier:
    def __init__(self,key,target):self.key,self.target=key,target
    def __call__(self,request):
        signature=request['authorization']
        return (target_from(request['target'])==self.target and type(signature) is str
                and hmac.compare_digest(signature,authorization_signature(self.key,request)))


class WorkspaceRPCServer:
    def __init__(self,keys,target,root,owner_uid,owner_gid,journal,authorize,*,runner=None,clock=time.time):
        if not isinstance(authorize,WorkspaceAuthorizationVerifier) or authorize.target!=target:reject()
        if authorize.key in (keys.request_key,keys.response_key):reject()
        self.keys,self.target,self.root,self.uid,self.gid=keys,target,root,owner_uid,owner_gid
        self.journal,self.authorize=journal,authorize
        self.runner=runner or ManagedProcess(enabled=True,timeout=7)
        self.clock=clock

    def run(self,request,action,proof=None):
        job={'action':action,'root':self.root,'owner_uid':self.uid,'owner_gid':self.gid,
             'target':asdict(self.target),'parent_pid':os.getpid(),'basis_id':request['basis_id'],
             'base_commit':request['base_commit'],'files':request['files'] if action=='INITIALIZE' else None,'proof':proof}
        return self.runner.run(job)

    def check_proof(self,request,proof):
        try:
            parsed=WorkspaceInitialization(**{**proof,'target':target_from(proof['target'])})
            uuid_text(parsed.workspace_id)
            if type(parsed.source_generation) is not int:reject()
            if (parsed.basis_id,parsed.target,parsed.base_commit,parsed.source_generation)!=(
                    request['basis_id'],self.target,request['base_commit'],request['generation']):reject()
        except Exception:reject()
        return parsed

    def handle(self,frame):
        request=unpack(frame,self.keys.request_key,REQUEST_DOMAIN,MAX_FRAME)
        validate(request,int(self.clock()))
        if target_from(request['target'])!=self.target or self.authorize(request) is not True:reject()
        action=request['operation'];row=self.journal.lookup(request['basis_id'])
        if action=='INITIALIZE':
            identity=fingerprint(request)
            if row:
                if row['fingerprint']!=identity or row['state']!='COMPLETE':reject()
                proof=row['proof'];self.check_proof(request,proof)
                if self.run(request,'VERIFY',proof).get('verified') is not True:reject()
            else:
                self.journal.claim(request['basis_id'],identity)
                try:
                    proof=self.run(request,'INITIALIZE')['proof'];self.check_proof(request,proof)
                    validate(request,int(self.clock()))
                    self.journal.finish(request['basis_id'],identity,proof)
                except Exception:
                    # commit応答喪失でもCOMPLETEをUNKNOWNへ戻さない。読み直せなければ不明。
                    current=self.journal.lookup(request['basis_id'])
                    if current and current['state']=='ACCEPTED':self.journal.finish(request['basis_id'],identity)
                    reject()
            result={'state':'COMPLETE','proof':proof}
        else:
            if row is None or row['state']!='COMPLETE':result={'state':'UNKNOWN','proof':None}
            else:
                proof=row['proof'];self.check_proof(request,proof)
                if action!='LOOKUP' and request['proof']!=proof:reject()
                checked=self.run(request,'UNCHANGED' if action=='UNCHANGED' else 'VERIFY',proof)
                if action=='LOOKUP' and checked.get('verified') is not True:reject()
                result={'state':'COMPLETE','proof':proof,'verified':checked.get('verified',False),
                        'unchanged':checked.get('unchanged',False)}
        validate(request,int(self.clock()))
        return pack({'binding':hashlib.sha256(canonical(request)).hexdigest(),'result':result},
                    self.keys.response_key,RESPONSE_DOMAIN,MAX_FRAME)


class RemoteWorkspaces:
    """署名・現在のDB認可・固定UDSを接続する管理側アダプター。自動再送しない。"""
    def __init__(self,keys,transport,issuer,*,member_id,project_id,collection_verifier=None,clock=time.time):
        self.keys,self.transport,self.issuer=keys,transport,issuer
        self.member_id,self.project_id=member_id,project_id
        self.collection_verifier,self.clock=collection_verifier,clock

    def call(self,target,base,basis,operation,request_id,files=None,proof=None):
        if target.student_id!=self.member_id or target.project_id!=self.project_id:reject()
        now=int(self.clock())
        request={'version':1,'operation':operation,'target':asdict(target),'basis_id':basis,
            'request_id':request_id,'base_commit':base,'generation':1,'files':files,'proof':proof,
            'issued_at':now,'expires_at':now+20,'nonce':str(uuid4()),'authorization':None}
        validate(request,now);request['authorization']=self.issuer.issue(request)
        response=self.transport.exchange(pack(request,self.keys.request_key,REQUEST_DOMAIN,MAX_FRAME),
            timeout=10,max_response=MAX_FRAME+4)
        result=unpack(response,self.keys.response_key,RESPONSE_DOMAIN,MAX_FRAME)
        if set(result)!={'binding','result'} or result['binding']!=hashlib.sha256(canonical(request)).hexdigest():reject()
        validate(request,int(self.clock()))
        return result['result']

    def initialize_for_request(self,target,base,basis,files,request_id):
        result=self.call(target,base,basis,'INITIALIZE',request_id,encode_snapshot(files))
        if result.get('state')!='COMPLETE':reject()
        p=result['proof'];proof=WorkspaceInitialization(**{**p,'target':target_from(p['target'])})
        if (proof.basis_id,proof.target,proof.base_commit,proof.source_generation)!=(basis,target,base,1):reject()
        uuid_text(proof.workspace_id);return proof

    def verify(self,proof):
        result=self.call(proof.target,proof.base_commit,proof.basis_id,'VERIFY',proof.basis_id,proof=asdict(proof))
        return result.get('state')=='COMPLETE' and result.get('verified') is True

    def verify_collection(self,basis,row):
        return self.collection_verifier is not None and self.collection_verifier(basis,row) is True

    def unchanged(self,basis):
        from app.submission_scope import SubmissionTarget
        target=SubmissionTarget(basis.team_id,basis.project_id,basis.task_id,basis.member_id,basis.folder_path)
        proof=WorkspaceInitialization(basis.id,target,basis.base_commit,basis.workspace_id,basis.source_generation)
        result=self.call(target,basis.base_commit,basis.id,'UNCHANGED',basis.id,proof=asdict(proof))
        return result.get('state')=='COMPLETE' and result.get('unchanged') is True

    def lookup_operation(self,operation,target):
        return self.call(target,operation.base_commit,operation.result_basis_id,'LOOKUP',operation.request_id)
