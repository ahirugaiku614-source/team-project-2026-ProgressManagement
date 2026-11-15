-- Apply manually to an existing PostgreSQL database after backup and review.
-- Never truncate or delete existing names. Abort if any name exceeds the limit.
BEGIN;
LOCK TABLE projects IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM projects WHERE char_length(name) > 100) THEN
        RAISE EXCEPTION 'Project names longer than 100 characters exist; review them before migration';
    END IF;
END $$;
ALTER TABLE projects ALTER COLUMN name TYPE VARCHAR(100);
COMMIT;
