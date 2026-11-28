"""ASGIと生成器が共有する設定schema。説明用の複合JSONをそのまま起動しない。"""
from pathlib import PurePosixPath
from app.submission_rpc import target_from,uuid_text,reject


def backend_settings(value):
    if (not isinstance(value,dict) or not {'workers'}<=set(value)<={'workers','collectors','gateway_enabled'}
            or not isinstance(value['workers'],list) or not isinstance(value.get('collectors',[]),list)
            or type(value.get('gateway_enabled',False)) is not bool):reject()
    common={'target','endpoint','owner_uid','request_key_file','response_key_file','authorization_key_file'}
    seen=set()
    for worker in value['workers']:
        if not isinstance(worker,dict) or set(worker)!=common:reject()
        validate_common(worker)
        target=target_from(worker['target']);identity=(target.student_id,target.project_id)
        if identity in seen:reject()
        seen.add(identity)
    seen=set()
    for collector in value.get('collectors',[]):
        if not isinstance(collector,dict) or set(collector)!=common|{'workspace_id','generation','collector_id','client_ledger'}:reject()
        validate_common(collector);uuid_text(collector['workspace_id'])
        if type(collector['generation']) is not int or collector['generation']<=0:reject()
        if not isinstance(collector['collector_id'],str) or not 1<=len(collector['collector_id'])<=128:reject()
        local_path(collector['client_ledger'])
        if collector['workspace_id'] in seen:reject()
        seen.add(collector['workspace_id'])
    return value


def local_path(value):
    if not isinstance(value,str) or not value.startswith('/') or '\x00' in value or '..' in PurePosixPath(value).parts or str(PurePosixPath(value))!=value:reject()


def validate_common(value):
    target_from(value['target'])
    if type(value['owner_uid']) is not int or value['owner_uid']<=0:reject()
    for key in ('endpoint','request_key_file','response_key_file','authorization_key_file'):local_path(value[key])
    if len({value[k] for k in ('request_key_file','response_key_file','authorization_key_file')})!=3:reject()
