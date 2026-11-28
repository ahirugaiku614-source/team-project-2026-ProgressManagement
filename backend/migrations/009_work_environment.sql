-- Stage 9の追加表だけを作る。既存データ・既存の基準・レビュー履歴は変更しない。
-- 005～008適用後、Stage 10でバックアップとPostgreSQL検証を経て明示適用する。
BEGIN;
CREATE TABLE work_editing_sessions (
    id varchar(36) PRIMARY KEY,
    basis_id varchar(36) NOT NULL REFERENCES work_bases(id) ON DELETE RESTRICT,
    member_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    expires_at timestamp NOT NULL,
    revoked_at timestamp
);
CREATE TABLE work_environment_operations (
    id varchar(36) PRIMARY KEY,
    request_id varchar(36) NOT NULL,
    member_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    team_id integer NOT NULL,
    project_id integer NOT NULL,
    task_id integer NOT NULL,
    previous_basis_id varchar(36),
    editing_session_id varchar(36),
    result_basis_id varchar(36),
    state varchar(24) NOT NULL,
    CONSTRAINT uq_work_environment_request UNIQUE(member_id, request_id)
);
CREATE TABLE work_editor_access (
    token_digest varchar(64) PRIMARY KEY,
    session_id varchar(36) NOT NULL REFERENCES work_editing_sessions(id) ON DELETE RESTRICT,
    workspace_id varchar(36) NOT NULL,
    expires_at timestamp NOT NULL
);
COMMIT;
