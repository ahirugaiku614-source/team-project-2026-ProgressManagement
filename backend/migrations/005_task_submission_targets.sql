-- 運用者が接続先とバックアップを確認して明示適用する。アプリ起動時には適用しない。
-- 既存Task/Project/Userの列・行は変更しない。未登録Taskは従来どおり利用可能。
-- 同名表があれば停止する。一度限りの移行であり、自動再試行/修復/再登録はしない。
BEGIN;
CREATE TABLE task_submission_targets (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    folder_path VARCHAR(512) NOT NULL,
    registered_by INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_task_submission_folder UNIQUE (project_id, student_id, folder_path),
    CONSTRAINT ck_task_submission_folder_length CHECK (length(folder_path) BETWEEN 1 AND 512)
);
CREATE INDEX ix_task_submission_targets_project_id ON task_submission_targets(project_id);
CREATE INDEX ix_task_submission_targets_student_id ON task_submission_targets(student_id);
COMMIT;
