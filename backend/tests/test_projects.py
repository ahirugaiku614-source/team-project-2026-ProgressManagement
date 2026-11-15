"""Run from backend: python -m unittest discover -s tests -v.

Uses an in-memory database by default, or an isolated PostgreSQL schema when
PROJECT_TEST_DATABASE_URL is set. Firebase's external token verifier alone is mocked;
the actual HTTP bearer dependency and team authorization remain active.
"""

import importlib
import os
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.db import database


def test_engine():
    database_url = os.environ.get("PROJECT_TEST_DATABASE_URL")
    if database_url:
        # A random private schema prevents touching existing application tables.
        schema = "project_test_" + uuid.uuid4().hex
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine.dispose()

        @event.listens_for(engine, "connect")
        def set_search_path(connection, record):
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
            connection.commit()

        engine.project_test_schema = schema
        return engine

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    return engine


def close_test_engine(engine):
    schema = getattr(engine, "project_test_schema", None)
    if schema:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


class ProjectAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = test_engine()
        # main.py creates tables at import time: redirect it before importing.
        # Skip credential loading without reading a real service account file.
        with patch.object(database, "engine", cls.engine), patch(
            "firebase_admin._apps", {"test": object()}
        ):
            cls.main = importlib.import_module("app.main")

    @classmethod
    def tearDownClass(cls):
        close_test_engine(cls.engine)

    def setUp(self):
        models.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.owner = models.User(firebase_uid="owner", email="owner@example.com")
        self.member = models.User(firebase_uid="member", email="member@example.com")
        self.outsider = models.User(firebase_uid="outsider", email="other@example.com")
        self.team = models.Team(name="Team A", invite_code="a")
        self.other_team = models.Team(name="Team B", invite_code="b")
        self.db.add_all([
            self.owner, self.member, self.outsider, self.team, self.other_team,
        ])
        self.db.flush()
        self.db.add_all([
            models.TeamMember(team_id=self.team.id, user_id=self.owner.id, role="owner"),
            models.TeamMember(team_id=self.team.id, user_id=self.member.id),
            models.TeamMember(team_id=self.other_team.id, user_id=self.outsider.id),
        ])
        self.db.commit()
        self.url = f"/api/teams/{self.team.id}/projects"

        def override_db():
            yield self.db

        self.main.app.dependency_overrides[database.get_db] = override_db
        self.addCleanup(self.main.app.dependency_overrides.clear)
        self.token_verifier = patch(
            "app.firebase.auth.verify_id_token", side_effect=self.decode_token
        )
        self.token_verifier.start()
        self.addCleanup(self.token_verifier.stop)
        self.client = TestClient(self.main.app, raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db.close()
        # Only the isolated test database/schema is cleared.
        models.Base.metadata.drop_all(self.engine)

    @staticmethod
    def decode_token(token):
        if token == "invalid":
            raise ValueError("Invalid token")
        return {"uid": token}

    @staticmethod
    def headers(uid="member"):
        return {"Authorization": f"Bearer {uid}"}

    def request(self, method, url=None, uid="member", **kwargs):
        return self.client.request(
            method, url or self.url, headers=self.headers(uid), **kwargs
        )

    def test_member_can_create_and_owner_can_list(self):
        response = self.request("POST", json={"name": "  開発プロジェクト  ", "description": "説明"})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.assertEqual(project["name"], "開発プロジェクト")
        self.assertEqual(project["description"], "説明")
        self.assertEqual(project["team_id"], self.team.id)
        self.assertEqual(project["created_by"], self.member.id)
        self.assertGreater(project["id"], 0)
        datetime.fromisoformat(project["created_at"])
        response = self.request("GET", uid="owner")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [project])
        stored = self.db.get(models.Project, project["id"])
        self.assertEqual(stored.team, self.team)
        self.assertEqual(stored.creator, self.member)
        self.assertIn(stored, self.team.projects)

    def test_owner_can_create_without_description(self):
        response = self.request("POST", uid="owner", json={"name": "Project"})
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.json()["description"])

    def test_empty_list(self):
        response = self.request("GET")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_only_target_team_is_listed_newest_first(self):
        old = datetime(2026, 1, 1)
        for name, team_id, created_at in [
            ("old", self.team.id, old),
            ("new", self.team.id, old + timedelta(days=1)),
            ("other", self.other_team.id, old),
        ]:
            self.db.add(models.Project(
                name=name, team_id=team_id, created_by=self.owner.id, created_at=created_at,
            ))
        self.db.commit()
        response = self.request("GET")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["name"] for p in response.json()], ["new", "old"])

    def test_nonmember_cannot_create_or_list(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                response = self.request(method, uid="outsider", json={"name": "Denied"})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db.query(models.Project).count(), 0)

    def test_membership_in_another_team_is_insufficient(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                response = self.request(
                    method, f"/api/teams/{self.other_team.id}/projects", json={"name": "Denied"},
                )
                self.assertEqual(response.status_code, 403)

    def test_unknown_user(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                response = self.request(method, uid="unknown", json={"name": "Denied"})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["detail"], "User not found")

    def test_unknown_team(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                response = self.request(method, "/api/teams/9999/projects", json={"name": "Denied"})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["detail"], "Team not found")

    def test_missing_or_invalid_authentication(self):
        for method in ("GET", "POST"):
            for headers in ({}, self.headers("invalid"), {"Authorization": "Basic invalid"}):
                with self.subTest(method=method, headers=headers):
                    response = self.client.request(
                        method, self.url, headers=headers, json={"name": "Denied"},
                    )
                    self.assertIn(response.status_code, (401, 403))
        self.assertEqual(self.db.query(models.Project).count(), 0)

    def test_invalid_names_and_description(self):
        for payload in ({}, {"name": ""}, {"name": " \t\n　"}, {"name": None},
                        {"name": 123}, {"name": "ok", "description": []}):
            with self.subTest(payload=payload):
                response = self.request("POST", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.db.query(models.Project).count(), 0)

    def test_cannot_spoof_creator_or_target_team(self):
        response = self.request("POST", json={
            "name": "Project", "created_by": self.outsider.id, "team_id": self.other_team.id,
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["created_by"], self.member.id)
        self.assertEqual(response.json()["team_id"], self.team.id)

    def test_database_error_rolls_back(self):
        with patch.object(self.db, "commit", side_effect=IntegrityError("test", {}, Exception())), patch.object(
            self.db, "rollback", wraps=self.db.rollback
        ) as rollback:
            response = self.request("POST", json={"name": "Failed"})
            self.assertEqual(response.status_code, 500)
            rollback.assert_called_once()
        self.assertEqual(self.db.query(models.Project).count(), 0)
        self.assertEqual(self.request("POST", json={"name": "Retry"}).status_code, 201)

    def test_project_foreign_keys_are_enforced(self):
        for team_id, created_by in ((9999, self.member.id), (self.team.id, 9999)):
            with self.subTest(team_id=team_id, created_by=created_by):
                self.db.add(models.Project(name="Invalid", team_id=team_id, created_by=created_by))
                with self.assertRaises(IntegrityError):
                    self.db.commit()
                self.db.rollback()

    def test_existing_team_and_task_apis(self):
        self.assertEqual(self.request("GET", "/api/me").status_code, 200)
        self.assertEqual(self.request("GET", "/api/teams").status_code, 200)
        self.assertEqual(self.request("GET", f"/api/teams/{self.team.id}/members").status_code, 200)
        url = f"/api/teams/{self.team.id}/tasks"
        response = self.request("POST", url, json={"title": "Task", "status": "TODO"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.request("GET", url).json()), 1)

    def test_existing_file_apis(self):
        response = self.request("POST", "/api/files", json={"name": "test.py", "language": "python"})
        self.assertEqual(response.status_code, 200, response.text)
        url = f"/api/files/{response.json()['id']}"
        self.assertEqual(self.request("GET", url).status_code, 200)
        response = self.request("PUT", url, json={
            "name": "test.py", "language": "python", "content": "print('ok')",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "print('ok')")
        self.assertEqual(len(self.request("GET", "/api/files").json()), 1)
        self.assertEqual(self.request("DELETE", url).status_code, 200)


class AdditiveSchemaTests(unittest.TestCase):
    def test_create_all_preserves_existing_data_and_is_repeatable(self):
        engine = test_engine()
        self.addCleanup(close_test_engine, engine)
        legacy_tables = [t for t in models.Base.metadata.sorted_tables if t.name != "projects"]
        models.Base.metadata.create_all(engine, tables=legacy_tables)
        with sessionmaker(bind=engine)() as db:
            user = models.User(firebase_uid="existing", email="existing@example.com")
            team = models.Team(name="Existing", invite_code="existing")
            db.add_all([user, team])
            db.flush()
            db.add_all([
                models.TeamMember(team_id=team.id, user_id=user.id),
                models.Task(title="Keep task", user_id=user.id, team_id=team.id),
                models.CodeFile(name="keep.py", language="python", content="keep", user_id=user.id),
            ])
            db.commit()
            models.Base.metadata.create_all(engine)
            models.Base.metadata.create_all(engine)
            self.assertIn("projects", inspect(engine).get_table_names())
            for model in (models.User, models.Team, models.TeamMember, models.Task, models.CodeFile):
                self.assertEqual(db.query(model).count(), 1)
            self.assertEqual(db.query(models.CodeFile).one().content, "keep")


if __name__ == "__main__":
    unittest.main()
