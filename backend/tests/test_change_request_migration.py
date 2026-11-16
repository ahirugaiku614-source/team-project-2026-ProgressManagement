import os
from pathlib import Path
import unittest
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import test_projects as fixtures
from app import models


@unittest.skipUnless(os.environ.get("PROJECT_TEST_DATABASE_URL"), "Requires isolated PostgreSQL")
class ChangeRequestMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = fixtures.test_engine()
        self.addCleanup(fixtures.close_test_engine, self.engine)
        tables = [table for table in models.Base.metadata.sorted_tables
                  if table.name not in ("change_requests", "change_request_reviews")]
        models.Base.metadata.create_all(self.engine, tables=tables)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.db.close)
        self.user = models.User(firebase_uid="existing", email="existing@example.com")
        self.team = models.Team(name="Existing", invite_code="existing")
        self.db.add_all([self.user, self.team])
        self.db.flush()
        self.project = models.Project(name="Keep project", team_id=self.team.id, created_by=self.user.id)
        self.db.add(self.project)
        self.db.commit()

    def migrate(self):
        script = (Path(__file__).parents[1] / "migrations/002_change_requests.sql").read_text(encoding="utf-8")
        connection = self.engine.raw_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(script)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def test_additive_migration_preserves_data_and_can_be_repeated(self):
        self.migrate()
        self.migrate()
        self.assertEqual(self.db.query(models.Project).one().name, "Keep project")
        self.assertEqual(self.db.query(models.User).count(), 1)
        self.assertEqual(self.db.query(models.Team).count(), 1)
        schema = inspect(self.engine)
        self.assertIn("ix_change_requests_project_created", {i["name"] for i in schema.get_indexes("change_requests")})
        self.assertEqual({fk["referred_table"] for fk in schema.get_foreign_keys("change_requests")}, {"projects", "users"})
        self.assertTrue(all(fk["options"]["ondelete"] == "RESTRICT" for fk in schema.get_foreign_keys("change_requests")))

    def test_constraints_reject_invalid_rows_and_duplicate_keys(self):
        self.migrate()
        values = dict(project_id=self.project.id, submitted_by=self.user.id, request_key=str(uuid.uuid4()),
                      title="Review", state="OPEN", snapshot_commit="a" * 40, base_commit="b" * 40,
                      git_ref="refs/change-requests/test/head")
        for invalid in ({"state": "MERGED"}, {"title": ""}, {"project_id": 9999},
                        {"submitted_by": 9999}, {"snapshot_commit": "short"}, {"description": "a" * 10001}):
            with self.subTest(invalid=invalid):
                self.db.add(models.ChangeRequest(**{**values, **invalid}))
                with self.assertRaises(SQLAlchemyError):
                    self.db.commit()
                self.db.rollback()
        self.db.add(models.ChangeRequest(**values))
        self.db.commit()
        self.db.add(models.ChangeRequest(**{**values, "git_ref": "refs/change-requests/other/head"}))
        with self.assertRaises(SQLAlchemyError):
            self.db.commit()
        self.db.rollback()
        with self.assertRaises(SQLAlchemyError):
            self.db.query(models.Project).filter_by(id=self.project.id).delete(synchronize_session=False)
            self.db.commit()
        self.db.rollback()
        self.assertEqual(self.db.query(models.ChangeRequest).count(), 1)

    def test_unexpected_existing_schema_aborts_without_modifying_data(self):
        from psycopg2 import Error
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE change_requests (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO change_requests VALUES (123)"))
        with self.assertRaises(Error):
            self.migrate()
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT id FROM change_requests")).scalar_one(), 123)
        self.assertEqual([c["name"] for c in inspect(self.engine).get_columns("change_requests")], ["id"])
        self.assertEqual(self.db.query(models.Project).one().name, "Keep project")

    def test_existing_table_with_missing_constraints_is_not_silently_accepted(self):
        from psycopg2 import Error
        self.migrate()
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE change_requests DROP CONSTRAINT ck_change_request_state"))
        with self.assertRaises(Error):
            self.migrate()
        self.assertEqual(self.db.query(models.Project).one().name, "Keep project")
