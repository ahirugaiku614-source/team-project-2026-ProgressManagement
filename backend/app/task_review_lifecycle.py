"""作業基準の不可逆失効。更新と同じDBトランザクションで記録する。

既存APIのORM更新だけでなく、一括UPDATE/DELETEもDBトリガーで対象にする。
履歴を消さず、ACTIVEだけをREVOKEDへ進める。元の担当へ戻しても復活しない。
本番PostgreSQLでは008 SQLの明示適用が必須であり、このモジュールは適用しない。
"""

# 条件の仕様を一か所に集める。SQLiteの合成試験とPostgreSQLの明示SQLを
# 同じ対象・変更条件から作るが、両DBのロック保証が等しいとは扱わない。
SPECS = (
    ('task_update', 'tasks', 'UPDATE',
     ('assignee_id', 'team_id'), 'task_id = OLD.id'),
    ('task_delete', 'tasks', 'DELETE', (), 'task_id = OLD.id'),
    ('target_update', 'task_submission_targets', 'UPDATE',
     ('student_id', 'project_id', 'folder_path', 'registered_by', 'created_at'), 'task_id = OLD.task_id'),
    ('target_delete', 'task_submission_targets', 'DELETE', (), 'task_id = OLD.task_id'),
    ('member_update', 'team_members', 'UPDATE',
     ('team_id', 'user_id', 'role', 'joined_at'), 'team_id = OLD.team_id AND member_id = OLD.user_id'),
    ('member_delete', 'team_members', 'DELETE', (), 'team_id = OLD.team_id AND member_id = OLD.user_id'),
    ('project_update', 'projects', 'UPDATE', ('team_id',), 'project_id = OLD.id'),
    ('workspace_update', 'work_workspace_revisions', 'UPDATE', ('generation',), 'workspace_id = OLD.workspace_id'),
    ('workspace_delete', 'work_workspace_revisions', 'DELETE', (), 'workspace_id = OLD.workspace_id'),
)


def trigger_sql(dialect):
    """明示移行・隔離DB用のDDL。既存データの補修や基準の再生成は行わない。"""
    statements = []
    for suffix, table, operation, columns, scope in SPECS:
        name = 'work_revoke_' + suffix
        different = (' IS NOT ' if dialect == 'sqlite' else ' IS DISTINCT FROM ')
        condition = ' OR '.join('OLD.' + c + different + 'NEW.' + c for c in columns) or 'TRUE'
        if suffix == 'task_update':
            condition += " OR (OLD.status" + different + "NEW.status AND NEW.status = 'DONE')"
        update = "UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND " + scope + ';'
        if dialect == 'sqlite':
            statements.append(f'CREATE TRIGGER {name} BEFORE {operation} ON {table} WHEN ({condition}) BEGIN {update} END;')
        else:
            statements.extend([
                f'CREATE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF ({condition}) THEN {update} END IF; RETURN ' + ('OLD' if operation == 'DELETE' else 'NEW') + '; END; $$;',
                f'CREATE TRIGGER {name} BEFORE {operation} ON {table} FOR EACH ROW EXECUTE FUNCTION {name}_fn();',
            ])
    return statements


def install_synthetic_lifecycle(connection):
    """新規合成テストからだけ明示呼出しする。metadataイベントには登録しない。

    従来の部分schema作成・繰返しcreate_allを変更しない。本番PostgreSQLでは
    呼べず、通常アプリケーションの起動・APIからも呼び出さない。
    """
    if connection.dialect.name != 'sqlite':raise ValueError('Synthetic SQLite only')
    for statement in trigger_sql('sqlite'):
        connection.exec_driver_sql(statement)


def require_lifecycle(db):
    """トリガー未適用の環境では新公開機能を拒否する。自動修復はしない。"""
    from sqlalchemy import text
    from app.project_storage import StorageUnavailable
    dialect = db.bind.dialect.name
    if dialect == 'sqlite':
        installed = set(db.execute(text("SELECT name,tbl_name FROM sqlite_master WHERE type='trigger'")))
    elif dialect == 'postgresql':
        installed = set(db.execute(text('SELECT t.tgname,c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid '
                                      'JOIN pg_namespace n ON n.oid=c.relnamespace '
                                      "WHERE NOT t.tgisinternal AND t.tgenabled IN ('O','A') AND n.nspname=current_schema()")))
    else:
        raise StorageUnavailable('Lifecycle schema unavailable')
    if not {('work_revoke_' + item[0],item[1]) for item in SPECS} <= installed:
        raise StorageUnavailable('Lifecycle schema unavailable')


def advance_workspace(db, workspace_id, generation):
    """信頼済み管理側用。世代は増加のみ。更新と失効は同じトランザクション。"""
    from app import models
    from app.project_storage import StorageConflict
    row = db.query(models.WorkWorkspaceRevision).filter_by(workspace_id=workspace_id).with_for_update().first()
    if row is None or type(generation) is not int or generation <= row.generation:
        raise StorageConflict('Invalid workspace generation')
    row.generation = generation
    db.flush()  # 呼出し元が更新全体をcommit/rollbackする。
