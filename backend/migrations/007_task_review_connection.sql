-- 明示適用専用。実DBへはこの工程で適用しない。履歴のTask IDはcascadeしない。
BEGIN;
CREATE TABLE work_bases (
 id VARCHAR(36) PRIMARY KEY,
 member_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 task_id INTEGER NOT NULL, workspace_id VARCHAR(36) NOT NULL UNIQUE,
 source_generation INTEGER NOT NULL, folder_path VARCHAR(512) NOT NULL,
 binding_version VARCHAR(64) NOT NULL, base_commit VARCHAR(40) NOT NULL,
 policy_version INTEGER NOT NULL, created_at TIMESTAMP NOT NULL,
 state VARCHAR(16) NOT NULL CHECK(state IN ('ACTIVE','ENDED','SUPERSEDED','REVOKED'))
);
CREATE TABLE work_capture_bindings (
 submission_id VARCHAR(36) PRIMARY KEY REFERENCES task_submissions(id) ON DELETE RESTRICT,
 basis_id VARCHAR(36) NOT NULL REFERENCES work_bases(id) ON DELETE RESTRICT,
 workspace_id VARCHAR(36) NOT NULL, source_generation INTEGER NOT NULL,
 editing_session_id VARCHAR(36) NOT NULL
);
CREATE TABLE task_deletion_intents (
 id VARCHAR(36) PRIMARY KEY,
 basis_id VARCHAR(36) NOT NULL REFERENCES work_bases(id) ON DELETE RESTRICT,
 submission_id VARCHAR(36) NOT NULL REFERENCES task_submissions(id) ON DELETE RESTRICT,
 member_id INTEGER NOT NULL, project_id INTEGER NOT NULL, task_id INTEGER NOT NULL,
 path VARCHAR(512) NOT NULL, blob VARCHAR(40) NOT NULL, mode VARCHAR(6) NOT NULL,
 binding_version VARCHAR(64) NOT NULL, policy_version INTEGER NOT NULL,
 confirmation_id VARCHAR(36) NOT NULL,
 CONSTRAINT uq_task_deletion_intent UNIQUE(basis_id,submission_id,path)
);
CREATE TABLE task_review_conversions (
 submission_id VARCHAR(36) PRIMARY KEY REFERENCES task_submissions(id) ON DELETE RESTRICT,
 basis_id VARCHAR(36) NOT NULL REFERENCES work_bases(id) ON DELETE RESTRICT,
 input_digest VARCHAR(64) NOT NULL, recipe TEXT NOT NULL,
 state VARCHAR(24) NOT NULL CHECK(state IN ('PREPARED','READY','NO_CHANGE','REVIEW_REQUIRED')),
 generated_commit VARCHAR(40),
 change_request_id INTEGER UNIQUE REFERENCES change_requests(id) ON DELETE RESTRICT,
 previous_request_id INTEGER REFERENCES change_requests(id) ON DELETE RESTRICT
);
COMMIT;
