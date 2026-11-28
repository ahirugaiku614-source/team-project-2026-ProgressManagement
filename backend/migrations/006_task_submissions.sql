-- Explicit operator migration only. Never run on application startup.
-- Task identifiers are historical values, not cascading foreign keys.
BEGIN;
CREATE TABLE submission_permits (
 id VARCHAR(36) PRIMARY KEY,
 token_digest VARCHAR(64) NOT NULL UNIQUE,
 student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 task_id INTEGER NOT NULL,
 editing_session_id VARCHAR(36) NOT NULL,
 folder_path VARCHAR(512) NOT NULL,
 purpose VARCHAR(32) NOT NULL CHECK (purpose = 'TASK_SUBMISSION'),
 created_at TIMESTAMP NOT NULL,
 expires_at TIMESTAMP NOT NULL CHECK (expires_at > created_at),
 consumed_at TIMESTAMP
);
CREATE TABLE task_submissions (
 id VARCHAR(36) PRIMARY KEY,
 student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
 task_id INTEGER NOT NULL,
 request_id VARCHAR(36) NOT NULL,
 editing_session_id VARCHAR(36) NOT NULL,
 permit_id VARCHAR(36) NOT NULL UNIQUE REFERENCES submission_permits(id) ON DELETE RESTRICT,
 permit_digest VARCHAR(64) NOT NULL,
 folder_path VARCHAR(512) NOT NULL,
 state VARCHAR(24) NOT NULL CHECK (state IN ('PROCESSING','SUBMITTED','REVIEW_REQUIRED')),
 git_ref VARCHAR(100) UNIQUE,
 commit_id VARCHAR(40),
 created_at TIMESTAMP NOT NULL,
 submitted_at TIMESTAMP,
 file_count INTEGER CHECK (file_count BETWEEN 1 AND 1000),
 total_bytes INTEGER CHECK (total_bytes BETWEEN 0 AND 10485760),
 CONSTRAINT uq_task_submission_request UNIQUE(student_id, task_id, request_id),
 CONSTRAINT ck_task_submission_receipt CHECK (state != 'SUBMITTED' OR
   (git_ref IS NOT NULL AND commit_id IS NOT NULL AND submitted_at IS NOT NULL AND file_count IS NOT NULL AND total_bytes IS NOT NULL))
);
COMMIT;
