"""準備済みA/B作業基準の束縛を読取り専用で出力する。作業内容の収集・修復はしない。"""
import argparse
import json
import os
from pathlib import Path
import sys


def export(fixture,db):
    from sqlalchemy import text
    from app import models
    from app.submission_targets import resolve_submission_target
    if db.bind.dialect.name!='postgresql':raise ValueError('Synthetic PostgreSQL required')
    db.execute(text('SET TRANSACTION READ ONLY'))
    marker=db.execute(text('SELECT fixture_id,purpose FROM stage10_synthetic_fixture')).fetchall()
    if marker!=[(fixture['fixture_id'],'SYNTHETIC_ONLY')]:raise ValueError('Fixture mismatch')
    result={}
    for label in ('A','B'):
        member=fixture['members'][label];actor=db.get(models.User,member['member_id'])
        target=resolve_submission_target(db,actor,team_id=fixture['team_id'],project_id=fixture['project_id'],task_id=member['task_id'])
        rows=db.query(models.WorkBasis).filter_by(member_id=actor.id,team_id=fixture['team_id'],
            project_id=fixture['project_id'],task_id=member['task_id'],state='ACTIVE').all()
        if len(rows)!=1:raise ValueError('Exactly one active basis required')
        basis=rows[0];revision=db.get(models.WorkWorkspaceRevision,basis.workspace_id)
        if revision is None or revision.generation!=basis.source_generation or basis.folder_path!=target.folder_path:raise ValueError('Stale basis')
        result[label]={key:getattr(basis,key) for key in ('id','member_id','project_id','task_id','workspace_id','source_generation','base_commit')}
    db.rollback()
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--fixture',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        if sys.platform!='linux' or os.geteuid()==0 or os.environ.get('STAGE10_SYNTHETIC_APPROVED')!='1':raise ValueError('Explicit approval required')
        from sqlalchemy.engine import make_url
        url=make_url(os.environ.get('DATABASE_URL',''))
        if url.get_backend_name()!='postgresql' or not (url.database or '').startswith('stage10_synthetic_') or url.host not in ('db','stage10-db'):raise ValueError('Dedicated DB required')
        from app.workspace_bootstrap import private_bytes
        from app.db.database import SessionLocal
        fixture=json.loads(private_bytes(args.fixture,16384))
        with SessionLocal() as db:result=export(fixture,db)
        from app.submission_ledger import private_parent
        output=Path(args.output);private_parent(output)
        fd=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump(result,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        print(json.dumps({'status':'BOUND_BASES_EXPORTED','count':2}))
    except Exception:raise SystemExit('作業基準の出力を拒否しました。再初期化や再収集は行いません。') from None
