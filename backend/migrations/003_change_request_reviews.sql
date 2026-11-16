-- 判定だけを別テーブルへ追加する。既存change_requestsのstateやデータは更新しない。
-- 子レコードが存在しない依頼は、アプリのレスポンスでPENDINGと表現する。
-- 必ずバックアップと接続先を確認し、運用者が手動で適用する。実DBへ自動適用しない。
BEGIN;
CREATE TABLE IF NOT EXISTS change_request_reviews (
    -- 主キーと外部キーを兼ね、同じ依頼に二つの判定が登録されることを防ぐ。
    change_request_id INTEGER PRIMARY KEY REFERENCES change_requests(id) ON DELETE RESTRICT,
    decision VARCHAR(20) NOT NULL,
    reviewer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reviewed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    reason VARCHAR(2000),
    CONSTRAINT ck_request_review_decision CHECK (decision IN ('APPROVED', 'CHANGES_REQUESTED')),
    CONSTRAINT ck_request_review_reason CHECK (
        (decision = 'APPROVED' AND reason IS NULL) OR
        (decision = 'CHANGES_REQUESTED' AND reason IS NOT NULL AND length(trim(reason)) BETWEEN 1 AND 2000)
    )
);

-- IF NOT EXISTSだけでは同名の別構造を受け入れてしまうため、必要な構造も検査する。
-- 想定外ならトランザクションを中止し、既存テーブルの削除・補正は行わない。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM (VALUES
            ('change_request_id', 'integer', true), ('decision', 'character varying(20)', true),
            ('reviewer_id', 'integer', true), ('reviewed_at', 'timestamp without time zone', true),
            ('reason', 'character varying(2000)', false)
        ) AS expected(name, type_name, required)
        LEFT JOIN pg_attribute a ON a.attrelid = 'change_request_reviews'::regclass
            AND a.attname = expected.name AND a.attnum > 0 AND NOT a.attisdropped
        WHERE a.attname IS NULL OR format_type(a.atttypid, a.atttypmod) <> expected.type_name
            OR a.attnotnull <> expected.required
    ) OR EXISTS (
        SELECT 1 FROM (VALUES ('ck_request_review_decision'), ('ck_request_review_reason')) AS expected(name)
        LEFT JOIN pg_constraint c ON c.conrelid = 'change_request_reviews'::regclass
            AND c.conname = expected.name AND c.contype = 'c' AND c.convalidated
        WHERE c.oid IS NULL
    ) OR EXISTS (
        SELECT 1 FROM (VALUES ('change_request_id', 'change_requests'), ('reviewer_id', 'users')) AS expected(column_name, parent)
        JOIN pg_attribute a ON a.attrelid = 'change_request_reviews'::regclass AND a.attname = expected.column_name
        LEFT JOIN pg_constraint c ON c.conrelid = a.attrelid AND c.contype = 'f'
            AND c.conkey = ARRAY[a.attnum] AND c.confrelid = to_regclass(expected.parent)
            AND c.confkey = ARRAY[(SELECT attnum FROM pg_attribute
                WHERE attrelid = to_regclass(expected.parent) AND attname = 'id')]
            AND c.confdeltype = 'r' AND c.convalidated
        WHERE c.oid IS NULL
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attname = 'change_request_id'
        WHERE c.conrelid = 'change_request_reviews'::regclass AND c.contype = 'p' AND c.conkey = ARRAY[a.attnum]
    ) THEN
        RAISE EXCEPTION 'Unexpected change_request_reviews schema; inspect before migration';
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_change_request_reviews_reviewer_id ON change_request_reviews (reviewer_id);
COMMIT;
