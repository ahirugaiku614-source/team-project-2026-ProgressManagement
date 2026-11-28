-- 既存セッションを方式Cへ自動変換しない。実DBへは別途承認後に適用する。
BEGIN;
ALTER TABLE work_editing_sessions ADD COLUMN capture_policy varchar(32) NOT NULL DEFAULT 'EDITOR_SAVED_V1';
ALTER TABLE work_editing_sessions ADD CONSTRAINT ck_editing_capture_policy
  CHECK (capture_policy IN ('EDITOR_SAVED_V1','SAVED_SNAPSHOT_V1'));
COMMIT;
