-- 実行先は運用者が確認する。稼働中DBへ起動時・テスト時に自動適用しない。
-- 既存の提出・承認記録を変更せず、統合の準備／確定記録だけを追加する。
BEGIN;
CREATE TABLE IF NOT EXISTS change_request_integrations (
    change_request_id INTEGER PRIMARY KEY REFERENCES change_requests(id) ON DELETE RESTRICT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    operation_id VARCHAR(36) NOT NULL CONSTRAINT uq_integration_operation UNIQUE,
    integrated_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status VARCHAR(16) NOT NULL,
    prepared_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    integrated_at TIMESTAMP WITHOUT TIME ZONE,
    main_before VARCHAR(40),
    main_after VARCHAR(40) NOT NULL,
    CONSTRAINT ck_integration_status CHECK (status IN ('PREPARED', 'COMPLETED')),
    CONSTRAINT ck_integration_time CHECK (
        (status = 'PREPARED' AND integrated_at IS NULL) OR
        (status = 'COMPLETED' AND integrated_at IS NOT NULL AND integrated_at >= prepared_at)
    ),
    CONSTRAINT ck_integration_operation CHECK (length(operation_id) = 36),
    CONSTRAINT ck_integration_commits CHECK (
        length(main_after) = 40 AND (main_before IS NULL OR (length(main_before) = 40 AND main_before <> main_after))
    )
);

-- 同名のCHECKが弱い式に置き換わっていても見逃さないよう、PostgreSQL自身が
-- 正規化した式を一時テーブルの期待値と比較する。この一時テーブルはCOMMITで消える。
CREATE TEMP TABLE expected_integration_constraints (
    change_request_id INTEGER, project_id INTEGER, operation_id VARCHAR(36), integrated_by INTEGER,
    status VARCHAR(16), prepared_at TIMESTAMP WITHOUT TIME ZONE, integrated_at TIMESTAMP WITHOUT TIME ZONE,
    main_before VARCHAR(40), main_after VARCHAR(40),
    CONSTRAINT ck_integration_status CHECK (status IN ('PREPARED', 'COMPLETED')),
    CONSTRAINT ck_integration_time CHECK (
        (status = 'PREPARED' AND integrated_at IS NULL) OR
        (status = 'COMPLETED' AND integrated_at IS NOT NULL AND integrated_at >= prepared_at)
    ),
    CONSTRAINT ck_integration_operation CHECK (length(operation_id) = 36),
    CONSTRAINT ck_integration_commits CHECK (
        length(main_after) = 40 AND (main_before IS NULL OR (length(main_before) = 40 AND main_before <> main_after))
    )
) ON COMMIT DROP;
CREATE UNIQUE INDEX expected_pending_project ON expected_integration_constraints(project_id) WHERE status = 'PREPARED';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES
            ('change_request_id', 'integer', true), ('project_id', 'integer', true),
            ('operation_id', 'character varying(36)', true), ('integrated_by', 'integer', true),
            ('status', 'character varying(16)', true), ('prepared_at', 'timestamp without time zone', true),
            ('integrated_at', 'timestamp without time zone', false),
            ('main_before', 'character varying(40)', false), ('main_after', 'character varying(40)', true)
        ) AS expected(name, type_name, required)
        LEFT JOIN pg_attribute a ON a.attrelid = 'change_request_integrations'::regclass
            AND a.attname = expected.name AND a.attnum > 0 AND NOT a.attisdropped
        WHERE a.attname IS NULL OR format_type(a.atttypid, a.atttypmod) <> expected.type_name
            OR a.attnotnull <> expected.required
    ) OR EXISTS (
        SELECT 1 FROM pg_constraint expected LEFT JOIN pg_constraint actual
            ON actual.conrelid = 'change_request_integrations'::regclass AND actual.conname = expected.conname
            AND actual.contype = 'c' AND actual.convalidated
        WHERE expected.conrelid = 'expected_integration_constraints'::regclass AND expected.contype = 'c'
            AND (actual.oid IS NULL OR pg_get_expr(actual.conbin, actual.conrelid) <> pg_get_expr(expected.conbin, expected.conrelid))
    ) OR EXISTS (
        SELECT 1 FROM (VALUES ('change_request_id', 'change_requests'), ('project_id', 'projects'), ('integrated_by', 'users'))
            AS expected(column_name, parent)
        JOIN pg_attribute a ON a.attrelid = 'change_request_integrations'::regclass AND a.attname = expected.column_name
        LEFT JOIN pg_constraint c ON c.conrelid = a.attrelid AND c.contype = 'f' AND c.conkey = ARRAY[a.attnum]
            AND c.confrelid = to_regclass(expected.parent) AND c.confdeltype = 'r' AND c.convalidated
            AND c.confkey = ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid = to_regclass(expected.parent) AND attname = 'id')]
        WHERE c.oid IS NULL
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attname = 'change_request_id'
        WHERE c.conrelid = 'change_request_integrations'::regclass AND c.contype = 'p' AND c.conkey = ARRAY[a.attnum]
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attname = 'operation_id'
        WHERE c.conrelid = 'change_request_integrations'::regclass AND c.contype = 'u' AND c.conkey = ARRAY[a.attnum]
    ) THEN
        RAISE EXCEPTION 'Unexpected integration schema; operator inspection required';
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_change_request_integrations_project_id ON change_request_integrations(project_id);
CREATE INDEX IF NOT EXISTS ix_change_request_integrations_integrated_by ON change_request_integrations(integrated_by);
CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_pending_project ON change_request_integrations(project_id) WHERE status = 'PREPARED';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attname = 'project_id'
        WHERE i.indexrelid = 'uq_integration_pending_project'::regclass AND i.indrelid = 'change_request_integrations'::regclass
            AND i.indisunique AND i.indisvalid AND i.indnkeyatts = 1 AND i.indkey[0] = a.attnum
            AND pg_get_expr(i.indpred, i.indrelid) = (SELECT pg_get_expr(indpred, indrelid)
                FROM pg_index WHERE indexrelid = 'expected_pending_project'::regclass)
    ) THEN
        RAISE EXCEPTION 'Unexpected pending integration index; operator inspection required';
    END IF;
END $$;
COMMIT;
