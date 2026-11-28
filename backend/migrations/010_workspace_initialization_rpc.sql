-- 実DBへは適用しない。既存受付と履歴は保持し、初期化前の基準だけ追加する。
BEGIN;
ALTER TABLE work_environment_operations ADD COLUMN base_commit varchar(40);
COMMIT;
