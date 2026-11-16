"""003は新しい試験DBの専用スキーマだけへ適用する。実DBへの移行処理ではない。"""

from datetime import datetime
import os
from pathlib import Path
import unittest
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

import test_projects as fixtures
from app import models
from app.db.database import create_startup_tables


@unittest.skipUnless(os.environ.get("PROJECT_TEST_DATABASE_URL"), "Requires isolated PostgreSQL migration database")
class ChangeRequestReviewMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = fixtures.test_engine()
        self.addCleanup(fixtures.close_test_engine, self.engine)
        # 現行main相当のテーブルを作り、003の追加テーブルだけをまだ作らない。
        create_startup_tables(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.db.close)
        self.user = models.User(firebase_uid="legacy", email="legacy@example.com", name="既存提出者")
        self.reviewer = models.User(firebase_uid="reviewer", email="reviewer@example.com", name="判定者")
        self.team = models.Team(name="Existing", invite_code="legacy")
        self.db.add_all([self.user, self.reviewer, self.team])
        self.db.flush()
        self.project = models.Project(name="Keep project", team_id=self.team.id, created_by=self.user.id)
        self.db.add(self.project)
        self.db.commit()
        # ORMの新しいselectin関係を移行前に読まないよう、旧データはCoreで登録・記録する。
        for index, state in enumerate(("OPEN", "CLOSED"), start=1):
            self.db.execute(models.ChangeRequest.__table__.insert().values(
                id=index, project_id=self.project.id, submitted_by=self.user.id, request_key=str(uuid.uuid4()),
                title="既存の提出", description="残す説明", state=state,
                snapshot_commit="a" * 40, base_commit="b" * 40, git_ref=f"refs/change-requests/legacy{index}/head",
                created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 2),
            ))
        self.db.commit()
        self.before = self.old_rows()

    def old_rows(self):
        return [dict(row) for row in self.db.execute(text("SELECT * FROM change_requests ORDER BY id")).mappings()]

    def migrate(self):
        script = (Path(__file__).parents[1] / "migrations/003_change_request_reviews.sql").read_text(encoding="utf-8")
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

    def test_existing_open_and_closed_requests_stay_unchanged_and_pending(self):
        self.migrate()
        self.migrate()
        self.assertEqual(self.old_rows(), self.before)
        rows = self.db.query(models.ChangeRequest).order_by(models.ChangeRequest.id).all()
        self.assertEqual([row.state for row in rows], ["OPEN", "CLOSED"])
        self.assertTrue(all(row.review_decision == "PENDING" and row.reviewer_id is None for row in rows))
        self.assertEqual(self.db.query(models.Project).one().name, "Keep project")

    def test_startup_does_not_implicitly_create_review_table(self):
        # 新規の一時DBで自動起動処理を繰り返しても、003の適用前には作成されない。
        create_startup_tables(self.engine)
        self.assertFalse(inspect(self.engine).has_table("change_request_reviews"))
        self.assertEqual(self.old_rows(), self.before)

    def test_review_results_survive_rerunning_migration(self):
        self.migrate()
        saved_time = datetime(2026, 11, 16, 0, 0)
        self.db.execute(models.ChangeRequestReview.__table__.insert().values(
            change_request_id=1, decision="APPROVED", reviewer_id=self.reviewer.id, reviewed_at=saved_time,
        ))
        self.db.commit()
        self.migrate()
        self.assertEqual(self.old_rows(), self.before)
        result = self.db.query(models.ChangeRequestReview).one()
        self.assertEqual((result.decision, result.reviewer_id, result.reviewed_at), ("APPROVED", self.reviewer.id, saved_time))

    def test_database_constraints_prevent_invalid_or_duplicate_decisions(self):
        self.migrate()
        values = dict(change_request_id=1, decision="APPROVED", reviewer_id=self.reviewer.id,
                      reviewed_at=datetime(2026, 11, 16), reason=None)
        invalid_values = [
            {"decision": "PENDING"}, {"decision": "MERGED"}, {"decision": None},
            {"decision": "APPROVED", "reason": "理由は差し戻し時のみ"},
            {"decision": "CHANGES_REQUESTED", "reason": None},
            {"decision": "CHANGES_REQUESTED", "reason": ""},
            {"decision": "CHANGES_REQUESTED", "reason": "   "},
            {"decision": "CHANGES_REQUESTED", "reason": "a" * 2001},
            {"reviewer_id": None}, {"reviewer_id": 9999}, {"change_request_id": 9999}, {"reviewed_at": None},
        ]
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaises(SQLAlchemyError):
                    self.db.execute(models.ChangeRequestReview.__table__.insert().values(**{**values, **invalid}))
                    self.db.commit()
                self.db.rollback()
        self.db.execute(models.ChangeRequestReview.__table__.insert().values(**values))
        self.db.commit()
        with self.assertRaises(SQLAlchemyError):
            self.db.execute(models.ChangeRequestReview.__table__.insert().values(**{**values, "reviewer_id": self.user.id}))
            self.db.commit()
        self.db.rollback()
        self.assertEqual(self.db.query(models.ChangeRequestReview).count(), 1)
        self.assertEqual(self.old_rows(), self.before)

    def test_foreign_keys_and_index_are_present_and_restrict_deletion(self):
        self.migrate()
        schema = inspect(self.engine)
        foreign_keys = schema.get_foreign_keys("change_request_reviews")
        self.assertEqual({fk["referred_table"] for fk in foreign_keys}, {"users", "change_requests"})
        self.assertTrue(all(fk["options"]["ondelete"] == "RESTRICT" for fk in foreign_keys))
        self.assertIn("ix_change_request_reviews_reviewer_id", {i["name"] for i in schema.get_indexes("change_request_reviews")})
        self.db.execute(models.ChangeRequestReview.__table__.insert().values(
            change_request_id=1, decision="APPROVED", reviewer_id=self.reviewer.id, reviewed_at=datetime.utcnow(),
        ))
        self.db.commit()
        for sql, identifier in (("DELETE FROM change_requests WHERE id = :id", 1),
                                ("DELETE FROM users WHERE id = :id", self.reviewer.id)):
            with self.subTest(sql=sql):
                with self.assertRaises(SQLAlchemyError):
                    self.db.execute(text(sql), {"id": identifier})
                    self.db.commit()
                self.db.rollback()
        self.assertEqual(self.old_rows(), self.before)

    def test_unexpected_existing_table_is_preserved_and_migration_aborts(self):
        from psycopg2 import Error
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE change_request_reviews (marker TEXT)"))
            connection.execute(text("INSERT INTO change_request_reviews VALUES ('keep')"))
        with self.assertRaises(Error):
            self.migrate()
        self.assertEqual(self.db.execute(text("SELECT marker FROM change_request_reviews")).scalar_one(), "keep")
        self.assertEqual(self.old_rows(), self.before)
