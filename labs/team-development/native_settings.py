"""fixtureの実IDから設定を生成する。旧説明用JSONの手編集・秘密値の埋込みを不要にする。"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import UUID


def generate(fixture,ids,bases=None):
    from app.workspace_settings import backend_settings
    from app.submission_rpc import target_from
    if set(ids)!={'a','b','manager','gateway','db'} or len(set(ids.values()))!=5 or any(type(v) is not int or not 0<v<=65536 for v in ids.values()):raise ValueError('Distinct non-root identities required')
    if str(UUID(fixture['fixture_id']))!=fixture['fixture_id']:raise ValueError('Fixture UUID required')
    workers=[];collectors=[];outputs={}
    for label in ('A','B'):
        small=label.lower();member=fixture['members'][label]
        target={'team_id':fixture['team_id'],'project_id':fixture['project_id'],
            'task_id':member['task_id'],'student_id':member['member_id'],'folder_path':member['folder_path']}
        target_from(target)
        def keys(prefix):return {name+'_key_file':prefix+'/'+name+'.key' for name in ('request','response','authorization')}
        worker={'target':target,'owner_uid':ids[small],'endpoint':'/run/workspace-'+small+'/initialize.sock',
            **{name+'_key_file':'/run/workspace-keys/'+small+'-'+name+'.key' for name in ('request','response','authorization')}}
        workers.append(worker)
        outputs[small+'-workspace.json']={'target':target,'owner_uid':ids[small],'owner_gid':ids[small],
            'client_uid':ids['manager'],'client_gid':ids['manager'],'root':'/managed',
            'journal':'/managed/private/initialization.sqlite','endpoint':'/run/workspace/initialize.sock',**keys('/run/keys')}
        if bases is not None:
            basis=bases[label]
            # 管理側DBから出力した束縛のみを使用する。利用者の任意UUID一覧は入力にしない。
            for key,value in [('member_id',member['member_id']),('project_id',fixture['project_id']),('task_id',member['task_id'])]:
                if basis[key]!=value:raise ValueError('Basis target mismatch')
            workspace=basis['workspace_id']
            if str(UUID(workspace))!=workspace or type(basis['source_generation']) is not int or basis['source_generation']<1:raise ValueError('Invalid generation')
            fixed={'target':target,'workspace_id':workspace,'generation':basis['source_generation'],
                'collector_id':'synthetic-collector-'+small,'owner_uid':ids[small]}
            collectors.append({**fixed,'endpoint':'/run/collector-'+small+'/collect.sock',
                'client_ledger':'/run/client-ledgers/'+small+'/requests.sqlite',
                **{name+'_key_file':'/run/workspace-keys/'+small+'-collect-'+name+'.key' for name in ('request','response','authorization')}})
            outputs[small+'-collector.json']={**fixed,'root':'/source','client_uid':ids['manager'],'client_gid':ids['manager'],
                'endpoint':'/run/collector/collect.sock','server_ledger':'/run/collector-ledger/requests.sqlite',**keys('/run/collector-keys')}
    workspace={'workers':workers,'collectors':collectors,'gateway_enabled':bases is not None}
    backend_settings(workspace)
    outputs['backend.json']={'deployment':'STAGE10_SYNTHETIC_ONLY','fixture_id':fixture['fixture_id'],
        'origin':'https://localhost:28443','auth_file':'/run/synthetic-auth/accounts.json',
        'auth_ledger':'/run/synthetic-auth/auth.sqlite','workspace':workspace}
    if bases is None:
        # 初期化前はUUIDを推測しない。準備画面とAPIだけを公開し、editor経路は拒否する。
        outputs['nginx.conf']=Path(__file__).with_name('nginx.prepare.conf.template').read_text(encoding='utf-8')
    if bases is not None:
        a,b=(bases[label]['workspace_id'] for label in ('A','B'))
        if a==b:raise ValueError('Separate workspaces required')
        outputs['socket-gateway.json']={'origin':'https://localhost:28443','manager':'http://backend:8000',
            'targets':{a:'ws://member-a:8080',b:'ws://member-b:8080'},'upstream_cookie_names':['code-server-session']}
        from render_gateway import render
        template=Path(__file__).with_name('nginx.conf.template').read_text(encoding='utf-8')
        outputs['nginx.conf']=render(template,a,b).replace('/index.html','/stage10d-native.html').replace('root /srv/web;','root /srv/web;\n    index stage10d-native.html;')
    return outputs


def write_bundle(output,configs):
    # 新しいディレクトリだけ。既存設定・既存volumeの更新はこの生成器では行わない。
    output=Path(output)
    if not output.is_absolute() or '..' in output.parts or any(p.is_symlink() for p in [output,*output.parents]):raise ValueError('Unsafe output')
    output.mkdir(mode=0o700)
    for name,value in configs.items():
        fd=os.open(output/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            stream.write(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,sort_keys=True));stream.flush();os.fsync(stream.fileno())
    # code-serverの認証はAPIのA/B認証と別の用途・別の秘密値とする。
    for small in ('a','b'):
        password=secrets.token_urlsafe(32)
        fd=os.open(output/(small+'-editor.yaml'),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write('bind-addr: 0.0.0.0:8080\nauth: password\npassword: '+password+'\ncert: false\ndisable-telemetry: true\ndisable-update-check: true\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--fixture',required=True);parser.add_argument('--ids',required=True);parser.add_argument('--bases');parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if sys.platform!='linux' or os.environ.get('STAGE10_SYNTHETIC_APPROVED')!='1':raise SystemExit('承認された合成Linux配置だけで実行してください。')
    try:
        configs=generate(json.loads(Path(args.fixture).read_text()),json.loads(Path(args.ids).read_text()),json.loads(Path(args.bases).read_text()) if args.bases else None)
        write_bundle(args.output,configs);print(json.dumps({'status':'GENERATED','files':len(configs)+2}))
    except Exception:raise SystemExit('設定生成を拒否しました。既存設定は変更しません。') from None
