"""004の検証先は専用PostgreSQL内のランダムスキーマ。実DBは使用禁止。"""

from datetime import datetime
import os
from pathlib import Path
import unittest
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

import test_change_request_review_migration as legacy
from app import models
from app.db.database import create_startup_tables


@unittest.skipUnless(os.environ.get("PROJECT_TEST_DATABASE_URL"), "Requires temporary PostgreSQL")
class IntegrationMigrationTests(unittest.TestCase):
    old_rows = legacy.ChangeRequestReviewMigrationTests.old_rows

    def setUp(self):
        legacy.ChangeRequestReviewMigrationTests.setUp(self)
        legacy.ChangeRequestReviewMigrationTests.migrate(self)
        self.db.add(models.ChangeRequestReview(change_request_id=1, decision="APPROVED",
                                              reviewer_id=self.reviewer.id, reviewed_at=datetime(2026, 11, 16)))
        self.db.commit()
        self.reviews_before = [dict(row) for row in self.db.execute(text("SELECT * FROM change_request_reviews")).mappings()]

    def migrate(self):
        script = (Path(__file__).parents[1] / "migrations/004_change_request_integrations.sql").read_text(encoding="utf-8")
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

    def values(self):
        return dict(change_request_id=1, project_id=self.project.id, operation_id=str(uuid.uuid4()),
                    integrated_by=self.reviewer.id, status="PREPARED", prepared_at=datetime(2026, 11, 16),
                    integrated_at=None, main_before=None, main_after="a" * 40)

    def assert_old_data(self):
        self.assertEqual(self.old_rows(), self.before)
        self.assertEqual([dict(row) for row in self.db.execute(text("SELECT * FROM change_request_reviews")).mappings()], self.reviews_before)

    def test_startup_excludes_integrations_even_when_repeated(self):
        create_startup_tables(self.engine)
        create_startup_tables(self.engine)
        self.assertFalse(inspect(self.engine).has_table("change_request_integrations"))
        self.assert_old_data()

    def test_migration_and_rerun_keep_submissions_approvals_and_integrations(self):
        self.migrate()
        self.db.execute(models.ChangeRequestIntegration.__table__.insert().values(**self.values()))
        self.db.commit()
        before = [dict(row) for row in self.db.execute(text("SELECT * FROM change_request_integrations")).mappings()]
        self.migrate()
        self.assertEqual([dict(row) for row in self.db.execute(text("SELECT * FROM change_request_integrations")).mappings()], before)
        self.assert_old_data()

    def test_constraints_and_unique_pending_project(self):
        self.migrate()
        values = self.values()
        for invalid in ({"status": "UNKNOWN"}, {"status": None}, {"status": "COMPLETED"},
                        {"integrated_at": datetime(2026, 11, 16)}, {"prepared_at": None},
                        {"status": "COMPLETED", "integrated_at": datetime(2026, 11, 15)},
                        {"operation_id": "short"}, {"operation_id": None}, {"main_after": "short"},
                        {"main_before": "a" * 40}, {"main_before": "short"}, {"main_after": None},
                        {"project_id": 99999}, {"integrated_by": 99999}, {"change_request_id": 99999}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SQLAlchemyError):
                    self.db.execute(models.ChangeRequestIntegration.__table__.insert().values(**{**values, **invalid}))
                    self.db.commit()
                self.db.rollback()
        self.db.execute(models.ChangeRequestIntegration.__table__.insert().values(**values))
        self.db.commit()
        for changes in ({"operation_id": str(uuid.uuid4())},
                        {"change_request_id": 2, "operation_id": str(uuid.uuid4())},
                        {"change_request_id": 2, "status": "COMPLETED", "integrated_at": datetime(2026, 11, 16)}):
            with self.subTest(changes=changes):
                with self.assertRaises(SQLAlchemyError):
                    self.db.execute(models.ChangeRequestIntegration.__table__.insert().values(**{**values, **changes}))
                    self.db.commit()
                self.db.rollback()
        # 先の操作を確定すると、同じProjectの次の準備を登録できる。
        self.db.execute(models.ChangeRequestIntegration.__table__.update().values(status="COMPLETED", integrated_at=datetime(2026, 11, 16)))
        self.db.commit()
        self.db.execute(models.ChangeRequestIntegration.__table__.insert().values(**{**values, "change_request_id": 2, "operation_id": str(uuid.uuid4())}))
        self.db.commit()
        self.assert_old_data()

    def test_fk_indexes_and_deletion_restrictions(self):
        self.migrate()
        schema = inspect(self.engine)
        self.assertEqual({fk["referred_table"] for fk in schema.get_foreign_keys("change_request_integrations")}, {"projects", "users", "change_requests"})
        self.assertTrue(all(fk["options"]["ondelete"] == "RESTRICT" for fk in schema.get_foreign_keys("change_request_integrations")))
        self.assertIn("uq_integration_pending_project", {i["name"] for i in schema.get_indexes("change_request_integrations")})
        self.db.execute(models.ChangeRequestIntegration.__table__.insert().values(**self.values()))
        self.db.commit()
        for table, identifier in (("projects", self.project.id), ("users", self.reviewer.id), ("change_requests", 1)):
            with self.assertRaises(SQLAlchemyError):
                # 識別子はこのテストの固定値。削除を試すのはランダムスキーマのfixtureのみ。
                self.db.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": identifier})
                self.db.commit()
            self.db.rollback()
        self.assert_old_data()

    def test_unexpected_existing_table_aborts_without_removing_it(self):
        from psycopg2 import Error
        with self.engine.begin() as conn:
            conn.execute(text("CREATE TABLE change_request_integrations (marker TEXT)"))
            conn.execute(text("INSERT INTO change_request_integrations VALUES ('keep')"))
        with self.assertRaises(Error):
            self.migrate()
        self.assertEqual(self.db.execute(text("SELECT marker FROM change_request_integrations")).scalar_one(), "keep")
        self.assert_old_data()

    def test_same_named_but_weakened_check_is_rejected(self):
        from psycopg2 import Error
        self.migrate()
        with self.engine.begin() as conn:
            conn.execute(text("ALTER TABLE change_request_integrations DROP CONSTRAINT ck_integration_status"))
            conn.execute(text("ALTER TABLE change_request_integrations ADD CONSTRAINT ck_integration_status CHECK (true)"))
        with self.assertRaises(Error):
            self.migrate()
        self.assert_old_data()

    def test_same_named_but_nonunique_pending_index_is_rejected(self):
        from psycopg2 import Error
        self.migrate()
        with self.engine.begin() as conn:
            conn.execute(text("DROP INDEX uq_integration_pending_project"))
            conn.execute(text("CREATE INDEX uq_integration_pending_project ON change_request_integrations(project_id)"))
        with self.assertRaises(Error):
            self.migrate()
        self.assert_old_data()

    def test_lookup_index_with_wrong_column_aborts_without_replacing_it(self):
        from psycopg2 import Error
        self.migrate()
        with self.engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_change_request_integrations_integrated_by"))
            conn.execute(text("CREATE INDEX ix_change_request_integrations_integrated_by ON change_request_integrations(status)"))
        with self.assertRaises(Error):
            self.migrate()
        index = next(i for i in inspect(self.engine).get_indexes("change_request_integrations")
                     if i["name"] == "ix_change_request_integrations_integrated_by")
        self.assertEqual(index["column_names"], ["status"])
        self.assert_old_data()

    def test_deferred_operation_uniqueness_is_not_accepted_on_rerun(self):
        from psycopg2 import Error
        self.migrate()
        with self.engine.begin() as conn:
            conn.execute(text("ALTER TABLE change_request_integrations DROP CONSTRAINT uq_integration_operation"))
            conn.execute(text("ALTER TABLE change_request_integrations ADD CONSTRAINT uq_integration_operation UNIQUE (operation_id) DEFERRABLE INITIALLY DEFERRED"))
        with self.assertRaises(Error):
            self.migrate()
        self.assert_old_data()
