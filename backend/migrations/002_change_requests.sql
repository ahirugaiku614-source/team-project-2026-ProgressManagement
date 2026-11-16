-- 既存のProject/User/Teamを変更・削除しない、追加テーブルだけの移行。
-- psql -v ON_ERROR_STOP=1 -f migrations/002_change_requests.sql で適用する。
BEGIN;
CREATE TABLE IF NOT EXISTS change_requests (
    id SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    submitted_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    request_key VARCHAR(36) NOT NULL,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    state VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    snapshot_commit VARCHAR(40) NOT NULL,
    base_commit VARCHAR(40) NOT NULL,
    git_ref VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    CONSTRAINT uq_change_request_key UNIQUE (project_id, submitted_by, request_key),
    CONSTRAINT uq_change_request_git_ref UNIQUE (git_ref),
    CONSTRAINT ck_change_request_state CHECK (state IN ('OPEN', 'CLOSED')),
    CONSTRAINT ck_change_request_title CHECK (length(title) BETWEEN 1 AND 200),
    CONSTRAINT ck_change_request_description CHECK (description IS NULL OR length(description) <= 10000),
    CONSTRAINT ck_change_request_commits CHECK (length(snapshot_commit) = 40 AND length(base_commit) = 40)
);
-- 同名の別構造のテーブルが既にある場合は、データを変更せず処理を止める。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES
            ('id', 'integer', true), ('project_id', 'integer', true),
            ('submitted_by', 'integer', true), ('request_key', 'character varying(36)', true),
            ('title', 'character varying(200)', true), ('description', 'text', false),
            ('state', 'character varying(16)', true), ('snapshot_commit', 'character varying(40)', true),
            ('base_commit', 'character varying(40)', true), ('git_ref', 'character varying(128)', true),
            ('created_at', 'timestamp without time zone', true), ('updated_at', 'timestamp without time zone', true)
        ) AS expected(name, type_name, required)
        LEFT JOIN pg_attribute a ON a.attrelid = 'change_requests'::regclass
            AND a.attname = expected.name AND a.attnum > 0 AND NOT a.attisdropped
        WHERE a.attname IS NULL OR format_type(a.atttypid, a.atttypmod) <> expected.type_name
            OR a.attnotnull <> expected.required
    ) THEN
        RAISE EXCEPTION 'Unexpected change_requests schema; inspect existing data before migration';
    END IF;
    IF EXISTS (
        SELECT 1 FROM (VALUES
            ('uq_change_request_key', 'u'), ('uq_change_request_git_ref', 'u'),
            ('ck_change_request_state', 'c'), ('ck_change_request_title', 'c'),
            ('ck_change_request_description', 'c'), ('ck_change_request_commits', 'c')
        ) AS expected(name, kind)
        LEFT JOIN pg_constraint c ON c.conrelid = 'change_requests'::regclass
            AND c.conname = expected.name AND c.contype::text = expected.kind AND c.convalidated
        WHERE c.oid IS NULL
    ) OR EXISTS (
        SELECT 1 FROM (VALUES ('project_id', 'projects'), ('submitted_by', 'users')) AS expected(column_name, parent)
        JOIN pg_attribute a ON a.attrelid = 'change_requests'::regclass AND a.attname = expected.column_name
        LEFT JOIN pg_constraint c ON c.conrelid = a.attrelid AND c.contype = 'f'
            AND c.conkey = ARRAY[a.attnum] AND c.confrelid = to_regclass(expected.parent)
            AND c.confkey = ARRAY[(SELECT attnum FROM pg_attribute
                WHERE attrelid = to_regclass(expected.parent) AND attname = 'id')]
            AND c.confdeltype = 'r' AND c.convalidated
        WHERE c.oid IS NULL
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attname = 'id'
        WHERE c.conrelid = 'change_requests'::regclass AND c.contype = 'p' AND c.conkey = ARRAY[a.attnum]
    ) THEN
        RAISE EXCEPTION 'Unexpected change_requests constraints; inspect before migration';
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_change_requests_project_created ON change_requests (project_id, created_at, id);
COMMIT;
