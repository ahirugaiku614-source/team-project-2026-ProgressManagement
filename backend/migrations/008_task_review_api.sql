-- PostgreSQLの明示移行専用。実DBにはこの工程で適用しない。
-- 更新側と基準失効を同じトランザクションで実施し、元の設定へ戻っても復活させない。
BEGIN;
CREATE TABLE work_workspace_revisions (
 workspace_id VARCHAR(36) PRIMARY KEY,
 generation INTEGER NOT NULL CHECK(generation > 0)
);
-- 既存基準の世代は引き継ぐ。基準refやworkspace自体は再作成しない。
INSERT INTO work_workspace_revisions(workspace_id,generation)
 SELECT workspace_id,source_generation FROM work_bases;
-- 失効履歴を保証できない旧基準を引き継いで有効扱いにしない。保存済み内容は消さない。
UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE';
CREATE TABLE work_review_previews (
 id VARCHAR(36) PRIMARY KEY,
 basis_id VARCHAR(36) NOT NULL REFERENCES work_bases(id) ON DELETE RESTRICT,
 submission_id VARCHAR(36) NOT NULL REFERENCES task_submissions(id) ON DELETE RESTRICT,
 input_digest VARCHAR(64) NOT NULL,
 expires_at TIMESTAMP NOT NULL
);

CREATE TABLE work_collection_diagnostics (
 submission_id VARCHAR(36) PRIMARY KEY REFERENCES task_submissions(id) ON DELETE RESTRICT,
 code VARCHAR(64) NOT NULL
);
CREATE FUNCTION work_revoke_task_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (OLD.assignee_id IS DISTINCT FROM NEW.assignee_id OR OLD.team_id IS DISTINCT FROM NEW.team_id OR (OLD.status IS DISTINCT FROM NEW.status AND NEW.status = 'DONE')) THEN UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND task_id = OLD.id; END IF; RETURN NEW; END; $$;
CREATE TRIGGER work_revoke_task_update BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION work_revoke_task_update_fn();
CREATE FUNCTION work_revoke_task_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (TRUE) THEN UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND task_id = OLD.id; END IF; RETURN OLD; END; $$;
CREATE TRIGGER work_revoke_task_delete BEFORE DELETE ON tasks FOR EACH ROW EXECUTE FUNCTION work_revoke_task_delete_fn();
CREATE FUNCTION work_revoke_target_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (OLD.student_id IS DISTINCT FROM NEW.student_id OR OLD.project_id IS DISTINCT FROM NEW.project_id OR OLD.folder_path IS DISTINCT FROM NEW.folder_path OR OLD.registered_by IS DISTINCT FROM NEW.registered_by OR OLD.created_at IS DISTINCT FROM NEW.created_at) THEN UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND task_id = OLD.task_id; END IF; RETURN NEW; END; $$;
CREATE TRIGGER work_revoke_target_update BEFORE UPDATE ON task_submission_targets FOR EACH ROW EXECUTE FUNCTION work_revoke_target_update_fn();
CREATE FUNCTION work_revoke_target_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (TRUE) THEN UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND task_id = OLD.task_id; END IF; RETURN OLD; END; $$;
CREATE TRIGGER work_revoke_target_delete BEFORE DELETE ON task_submission_targets FOR EACH ROW EXECUTE FUNCTION work_revoke_target_delete_fn();
CREATE FUNCTION work_revoke_member_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (OLD.team_id IS DISTINCT FROM NEW.team_id OR OLD.user_id IS DISTINCT FROM NEW.user_id OR OLD.role IS DISTINCT FROM NEW.role OR OLD.joined_at IS DISTINCT FROM NEW.joined_at) THEN UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND team_id = OLD.team_id AND member_id = OLD.user_id; END IF; RETURN NEW; END; $$;
CREATE TRIGGER work_revoke_member_update BEFORE UPDATE ON team_members FOR EACH ROW EXECUTE FUNCTION work_revoke_member_update_fn();
CREATE FUNCTION work_revoke_member_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (TRUE) THEN UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND team_id = OLD.team_id AND member_id = OLD.user_id; END IF; RETURN OLD; END; $$;
CREATE TRIGGER work_revoke_member_delete BEFORE DELETE ON team_members FOR EACH ROW EXECUTE FUNCTION work_revoke_member_delete_fn();
CREATE FUNCTION work_revoke_project_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (OLD.team_id IS DISTINCT FROM NEW.team_id) THEN UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND project_id = OLD.id; END IF; RETURN NEW; END; $$;
CREATE TRIGGER work_revoke_project_update BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION work_revoke_project_update_fn();
CREATE FUNCTION work_revoke_workspace_update_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (OLD.generation IS DISTINCT FROM NEW.generation) THEN UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND workspace_id = OLD.workspace_id; END IF; RETURN NEW; END; $$;
CREATE TRIGGER work_revoke_workspace_update BEFORE UPDATE ON work_workspace_revisions FOR EACH ROW EXECUTE FUNCTION work_revoke_workspace_update_fn();
CREATE FUNCTION work_revoke_workspace_delete_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF (TRUE) THEN UPDATE work_bases SET state = 'REVOKED' WHERE state = 'ACTIVE' AND workspace_id = OLD.workspace_id; END IF; RETURN OLD; END; $$;
CREATE TRIGGER work_revoke_workspace_delete BEFORE DELETE ON work_workspace_revisions FOR EACH ROW EXECUTE FUNCTION work_revoke_workspace_delete_fn();
COMMIT;
