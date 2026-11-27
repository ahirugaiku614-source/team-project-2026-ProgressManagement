
BEGIN;

-- チーム作成者のIDを追加
ALTER TABLE teams
ADD COLUMN IF NOT EXISTS created_by
INTEGER REFERENCES users(id);

-- チーム作成時の操作IDを追加
ALTER TABLE teams
ADD COLUMN IF NOT EXISTS request_key VARCHAR(36);

-- プロジェクト作成時の操作IDを追加
ALTER TABLE projects
ADD COLUMN IF NOT EXISTS request_key VARCHAR(36);

-- チームの重複作成を防止
CREATE UNIQUE INDEX IF NOT EXISTS
uq_team_registration_key
ON teams (created_by, request_key);

-- プロジェクトの重複作成を防止
CREATE UNIQUE INDEX IF NOT EXISTS
uq_project_registration_key
ON projects (team_id, created_by, request_key);

COMMIT;
