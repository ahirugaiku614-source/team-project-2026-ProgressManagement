"""合成専用ASGI入口。通常main/stage10_asgiはこの入口を読み込まない。"""
import json
import os
import sys


def load_synthetic_settings(path):
    if (os.environ.get('STAGE10_SYNTHETIC_APPROVED')!='1' or sys.platform!='linux'
            or os.geteuid()==0 or 'app.firebase' in sys.modules or 'firebase_admin' in sys.modules):
        raise RuntimeError('Isolated synthetic bootstrap required')
    from app.workspace_bootstrap import private_bytes
    from app.workspace_settings import backend_settings
    from sqlalchemy.engine import make_url
    value=json.loads(private_bytes(path,65536))
    expected={'deployment','fixture_id','origin','auth_file','auth_ledger','workspace'}
    if set(value)!=expected or value['deployment']!='STAGE10_SYNTHETIC_ONLY':raise RuntimeError('Invalid synthetic schema')
    if value['auth_file']!='/run/synthetic-auth/accounts.json' or value['auth_ledger']!='/run/synthetic-auth/auth.sqlite':
        raise RuntimeError('Fixed synthetic vault required')
    url=make_url(os.environ.get('DATABASE_URL',''))
    if url.get_backend_name()!='postgresql' or not (url.database or '').startswith('stage10_synthetic_') or url.host not in ('db','stage10-db'):
        raise RuntimeError('Dedicated synthetic PostgreSQL required')
    backend_settings(value['workspace'])
    from app.submission_rpc import uuid_text
    uuid_text(value['fixture_id'])
    return value


def bootstrap():
    try:
        settings=load_synthetic_settings(os.environ.get('STAGE10_SYNTHETIC_SETTINGS_FILE',''))
        from app.db.database import engine
        from sqlalchemy import text
        # DB名だけを分離証拠にしない。専用fixtureが作ったmarkerも一致させる。
        with engine.connect() as db:
            marker=db.execute(text('SELECT fixture_id,purpose FROM stage10_synthetic_fixture')).fetchall()
            if marker!=[(settings['fixture_id'],'SYNTHETIC_ONLY')]:raise RuntimeError('Synthetic fixture mismatch')
        from app.workspace_bootstrap import private_bytes,install_backend
        from app.synthetic_auth import SyntheticAuth
        from app.synthetic_application import build_synthetic_app,bound_sessions
        accounts=json.loads(private_bytes(settings['auth_file'],16384))
        auth=SyntheticAuth(settings['auth_ledger'],accounts)
        # mainではなく認証非依存の共通定義を使う。製品DB・Git処理は共通。
        from app.application_routes import app as base_app,get_project_team_member
        result=build_synthetic_app(base_app,auth,settings['origin'])
        workspace=settings['workspace']
        install_backend(result,get_project_team_member,workspace['workers'],enabled=True,
            collectors=workspace.get('collectors',[]),gateway_enabled=workspace.get('gateway_enabled',False),
            sessions_factory=lambda db,request:bound_sessions(db,auth,getattr(request.state,'synthetic_digest',None)))
        if 'app.firebase' in sys.modules or 'firebase_admin' in sys.modules:raise RuntimeError('Unexpected Firebase import')
        return result
    except Exception:
        raise RuntimeError('Synthetic isolated startup refused; verify private settings and schema') from None


app=bootstrap()
