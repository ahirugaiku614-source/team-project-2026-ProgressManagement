"""新規合成DBと管理Gitだけを初期化する。既存行・領域を上書きしない。"""
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import uuid4


def seed_database(db,accounts,fixture_id):
    """認証以外は既存modelを使う。既存アプリデータがあれば空DBと推測しない。"""
    from app import models
    from sqlalchemy import text,inspect
    if db.bind.dialect.name=='postgresql':
        # 空DB確認とfixture作成を直列化する。別の初期化が動作中なら待ち続けない。
        db.execute(text("SET LOCAL lock_timeout='2s'"))
        if not db.execute(text('SELECT pg_try_advisory_xact_lock(104001)')).scalar():
            raise ValueError('Fixture initialization is already running')
    for model in (models.User,models.Team,models.TeamMember,models.Project,models.Task,models.WorkBasis,models.TaskSubmission):
        if db.query(model).first() is not None:raise ValueError('Existing application data; use a new database')
    if inspect(db.bind).has_table('stage10_synthetic_fixture'):raise ValueError('Existing fixture marker')
    users={}
    for label in ('A','B'):
        value=accounts[label]
        user=models.User(firebase_uid=value['firebase_uid'],email=value['email'],name=value['name'])
        db.add(user);users[label]=user
    team=models.Team(name='合成実機検証チーム',invite_code=secrets.token_urlsafe(12))
    db.add(team);db.flush()
    for label in ('A','B'):
        db.add(models.TeamMember(team_id=team.id,user_id=users[label].id,role='owner' if label=='B' else 'member'))
    project=models.Project(name='合成コード共有MVP',team_id=team.id,created_by=users['B'].id)
    db.add(project);db.flush();tasks={}
    for label in ('A','B'):
        task=models.Task(title='メンバー'+label+'の開発タスク',user_id=users['B'].id,
            assignee_id=users[label].id,team_id=team.id,status=models.TaskStatus.TODO)
        db.add(task);db.flush()
        db.add(models.TaskSubmissionTarget(task_id=task.id,project_id=project.id,student_id=users[label].id,
            folder_path='tasks/member-'+label.lower(),registered_by=users['B'].id))
        tasks[label]=task
    db.execute(text('CREATE TABLE stage10_synthetic_fixture (fixture_id varchar(36) PRIMARY KEY, purpose varchar(32) NOT NULL)'))
    db.execute(text("INSERT INTO stage10_synthetic_fixture VALUES (:id,'SYNTHETIC_ONLY')"),{'id':fixture_id})
    db.commit()
    return {'fixture_id':fixture_id,'team_id':team.id,'project_id':project.id,
            'members':{label:{'member_id':users[label].id,'task_id':tasks[label].id,
                'folder_path':'tasks/member-'+label.lower()} for label in ('A','B')}}


def seed_git(storage,receipt):
    from app.submission_git import SubmissionReviewGit
    storage.initialize(receipt['project_id'],receipt['members']['B']['member_id'])
    with SubmissionReviewGit(storage).session(receipt['project_id']) as git:
        if git.read_ref(('heads','main')) is not None:raise ValueError('Existing shared branch')
        files={'tasks/member-a/demo.txt':('100644',b'synthetic baseline A\n'),
               'tasks/member-b/demo.txt':('100644',b'synthetic baseline B\n')}
        tree=git.tree(files);commit=git.run('commit-tree',tree,data=b'Synthetic fixture baseline\n').decode().strip()
        with git.new_directory(git.shared,'refs') as refs,git.new_directory(refs,'heads') as heads:
            with git.git_lock(heads,'main.lock'):git.write_once(heads,'main',(commit+'\n').encode())
        return commit


def provision_fixture(directory,git_root):
    from sqlalchemy.engine import make_url
    if (sys.platform!='linux' or os.geteuid()==0 or os.environ.get('STAGE10_SYNTHETIC_APPROVED')!='1'):
        raise ValueError('Approved non-root synthetic environment required')
    url=make_url(os.environ.get('DATABASE_URL',''))
    if url.get_backend_name()!='postgresql' or not (url.database or '').startswith('stage10_synthetic_') or url.host not in ('db','stage10-db'):
        raise ValueError('Dedicated synthetic PostgreSQL required')
    from app.submission_ledger import private_parent
    from app.synthetic_auth import password_record,SyntheticAuth
    from app.db.database import SessionLocal
    from app.project_storage import ProjectStorage
    directory=Path(directory);private_parent(directory/'guard')
    if any(directory.iterdir()):raise ValueError('New empty fixture vault required')
    def write(name,value):
        fd=os.open(directory/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True);stream.flush();os.fsync(stream.fileno())
    fixture=str(uuid4());passwords={label:secrets.token_urlsafe(24) for label in ('A','B')}
    accounts={label:{'firebase_uid':'stage10-synthetic-'+label.lower()+'-'+fixture[:8],
        'email':'member-'+label.lower()+'@example.com','name':'合成メンバー'+label,
        **password_record(passwords[label])} for label in ('A','B')}
    # 受付を先に残す。DB commit応答喪失・Git失敗でも、同じvaultを自動再利用しない。
    write('admitted.json',{'fixture_id':fixture,'state':'ADMITTED'})
    write('accounts.json',accounts);write('operator-credentials.json',passwords)
    SyntheticAuth.provision(directory/'auth.sqlite',accounts)
    with SessionLocal() as db:receipt=seed_database(db,accounts,fixture)
    receipt['base_commit']=seed_git(ProjectStorage(git_root),receipt)
    write('fixture.json',receipt)
    return receipt


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--private-directory',required=True);parser.add_argument('--git-root',required=True)
    args=parser.parse_args()
    try:
        value=provision_fixture(args.private_directory,args.git_root)
        print(json.dumps({'status':'CREATED','fixture_id':value['fixture_id']}))
    except Exception:raise SystemExit('合成fixture作成を拒否しました。受付と既存DB/Gitを保全して確認してください。') from None
