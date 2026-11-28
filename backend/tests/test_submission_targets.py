"""既存HTTP認証は試験用Firebase verifier、DBは隔離メモリDBだけを使用する。"""
from dataclasses import asdict
from pathlib import Path
import sqlite3
import unittest

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

import test_projects as fixtures
from app import models
from app.db import database
from app.submission_targets import (
    SubmissionTarget, TargetConflict, TargetForbidden, TargetInvalid,
    create_submission_target_resolver, normalize_folder_path,
    register_submission_target, resolve_submission_target,
)


class PathTests(unittest.TestCase):
    def test_canonical_relative_paths(self):
        for path in ('assignment-01', '課題1/src', 'course/task_1.v2'):
            self.assertEqual(normalize_folder_path(path), path)

    def test_noncanonical_and_outside_paths(self):
        for path in ('', '.', '..', '/tmp/task', '../task', 'a/../b', 'a/./b',
                     'a//b', 'a/', 'C:/task', r'C:\task', r'\\server\share',
                     'a\\b', 'a\x00b', 'a\nb', '.git', 'a/.env', '%2e%2e/task',
                     'e\u0301', 'x' * 256, '/'.join(['x'] * 21), '界' * 171):
            with self.subTest(path=repr(path)):
                with self.assertRaises(TargetInvalid):
                    normalize_folder_path(path)

    def test_invalid_types_and_identifiers(self):
        for value in (None, 1, b'task', ['task']):
            with self.assertRaises(TargetInvalid):
                normalize_folder_path(value)
        for value in (True, 0, -1, '1', 2**31):
            with self.assertRaises(TargetInvalid):
                SubmissionTarget(value, 1, 1, 1, 'task')


class TargetTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.ProjectAPITests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.ProjectAPITests.tearDownClass.__func__)
    tearDown = fixtures.ProjectAPITests.tearDown
    request = fixtures.ProjectAPITests.request
    headers = staticmethod(fixtures.ProjectAPITests.headers)
    decode_token = staticmethod(fixtures.ProjectAPITests.decode_token)

    def setUp(self):
        fixtures.ProjectAPITests.setUp(self)
        self.project = models.Project(name='Assignment project', team_id=self.team.id, created_by=self.owner.id)
        self.foreign = models.Project(name='Other project', team_id=self.other_team.id, created_by=self.outsider.id)
        self.task = models.Task(title='Assignment', user_id=self.owner.id, team_id=self.team.id,
                                assignee_id=self.member.id)
        self.db.add_all([self.project, self.foreign, self.task])
        self.db.commit()

    def register(self, **values):
        args = dict(team_id=self.team.id, project_id=self.project.id, task_id=self.task.id,
                    student_id=self.member.id, folder_path='assignments/task-1')
        args.update(values)
        return register_submission_target(self.db, self.owner, **args)

    def resolve(self, actor=None, **values):
        args = dict(team_id=self.team.id, project_id=self.project.id, task_id=self.task.id)
        args.update(values)
        return resolve_submission_target(self.db, actor or self.member, **args)

    def test_registered_assignee_is_authorized(self):
        row = self.register()
        self.db.commit()
        result = self.resolve()
        self.assertEqual(asdict(result), dict(team_id=self.team.id, project_id=self.project.id,
                         task_id=self.task.id, student_id=self.member.id, folder_path='assignments/task-1'))
        self.assertEqual(row.registered_by, self.owner.id)

    def test_legacy_task_is_not_implicitly_a_submission_target(self):
        with self.assertRaises(TargetConflict):
            self.resolve()
        self.assertEqual(self.db.get(models.Task, self.task.id).title, 'Assignment')

    def test_only_owner_can_register(self):
        with self.assertRaises(TargetForbidden):
            register_submission_target(self.db, self.member, team_id=self.team.id,
                project_id=self.project.id, task_id=self.task.id, student_id=self.member.id, folder_path='task')
        self.assertEqual(self.db.query(models.TaskSubmissionTarget).count(), 0)

    def test_owner_cannot_submit_for_student(self):
        self.register()
        with self.assertRaises(TargetForbidden):
            self.resolve(self.owner)

    def test_other_team_student_is_rejected(self):
        self.register()
        with self.assertRaises(TargetForbidden):
            self.resolve(self.outsider)

    def test_cross_team_project_or_task_is_rejected(self):
        with self.assertRaises(TargetInvalid):
            self.register(project_id=self.foreign.id)
        foreign_task = models.Task(title='Other', user_id=self.outsider.id, team_id=self.other_team.id,
                                   assignee_id=self.outsider.id)
        self.db.add(foreign_task)
        self.db.flush()
        with self.assertRaises(TargetInvalid):
            self.register(task_id=foreign_task.id)

    def test_unassigned_or_other_assignee_is_rejected(self):
        with self.assertRaises(TargetForbidden):
            self.register(student_id=self.owner.id)
        self.task.assignee_id = None
        self.db.flush()
        with self.assertRaises(TargetForbidden):
            self.register()

    def test_duplicate_is_idempotent_but_rebinding_is_rejected(self):
        first = self.register()
        self.assertIs(self.register(), first)
        with self.assertRaises(TargetConflict):
            self.register(folder_path='other')
        self.assertEqual(self.db.query(models.TaskSubmissionTarget).count(), 1)

    def test_overlapping_paths_are_rejected(self):
        self.register()
        another = models.Task(title='Other', user_id=self.owner.id, team_id=self.team.id,
                              assignee_id=self.member.id)
        self.db.add(another)
        self.db.flush()
        for path in ('assignments', 'assignments/task-1', 'assignments/task-1/src'):
            with self.assertRaises(TargetConflict):
                self.register(task_id=another.id, folder_path=path)
        self.register(task_id=another.id, folder_path='assignments/task-10')

    def test_removing_membership_revokes_access(self):
        self.register()
        self.db.query(models.TeamMember).filter_by(team_id=self.team.id, user_id=self.member.id).delete()
        self.db.flush()
        with self.assertRaises(TargetForbidden):
            self.resolve()

    def test_legacy_reassignment_does_not_grant_submission_rights(self):
        self.register()
        self.db.commit()
        url = f'/api/teams/{self.team.id}/tasks/{self.task.id}'
        response = self.request('PUT', url, json=dict(title='Assignment', status='IN_PROGRESS',
                                assignee_id=self.owner.id))
        self.assertEqual(response.status_code, 200)
        for user in (self.owner, self.member):
            with self.assertRaises(TargetForbidden):
                self.resolve(user)

    def test_wrong_project_binding_and_bad_stored_path_are_rejected(self):
        row = self.register()
        second = models.Project(name='Second', team_id=self.team.id, created_by=self.owner.id)
        self.db.add(second)
        self.db.flush()
        with self.assertRaises(TargetInvalid):
            self.resolve(project_id=second.id)
        row.folder_path = '../outside'
        self.db.flush()
        with self.assertRaises(TargetInvalid):
            self.resolve()

    def test_outside_path_registration_leaves_no_binding(self):
        with self.assertRaises(TargetInvalid):
            self.register(folder_path='/outside/task')
        self.assertEqual(self.db.query(models.TaskSubmissionTarget).count(), 0)

    def test_tampered_student_binding_is_not_accepted(self):
        row = self.register()
        row.student_id = self.owner.id
        self.db.flush()
        for actor in (self.member, self.owner):
            with self.assertRaises(TargetForbidden):
                self.resolve(actor)

    def test_deleting_task_revokes_binding_without_changing_other_tasks(self):
        self.register()
        self.db.commit()
        response = self.request('DELETE', f'/api/teams/{self.team.id}/tasks/{self.task.id}', uid='owner')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.query(models.TaskSubmissionTarget).count(), 0)

    def test_legacy_task_and_file_apis_keep_their_contracts(self):
        response = self.request('POST', f'/api/teams/{self.team.id}/tasks',
                                json=dict(title='Legacy', status='TODO', assignee_id=self.member.id))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('folder_path', response.json())
        self.assertNotIn('project_id', response.json())
        file = self.request('POST', '/api/files', json=dict(name='legacy.py', language='python', content=''))
        self.assertEqual(file.status_code, 200)
        self.assertEqual(self.db.query(models.TaskSubmissionTarget).count(), 0)

    def test_resolver_uses_existing_auth_and_membership_without_a_production_api(self):
        self.register()
        self.db.commit()
        app = FastAPI()
        resolver = create_submission_target_resolver(self.main.get_project_team_member, self.main.get_accessible_project)

        @app.get('/probe/{team_id}/{project_id}/{task_id}')
        def probe(target=Depends(resolver)):
            return asdict(target)

        def db_override():
            yield self.db

        app.dependency_overrides[database.get_db] = db_override
        with TestClient(app) as client:
            url = f'/probe/{self.team.id}/{self.project.id}/{self.task.id}'
            self.assertIn(client.get(url).status_code, (401, 403))
            self.assertEqual(client.get(url, headers=self.headers('invalid')).status_code, 401)
            self.assertEqual(client.get(url, headers=self.headers('outsider')).status_code, 403)
            self.assertEqual(client.get(url, headers=self.headers('owner')).status_code, 403)
            self.assertEqual(client.get(url, headers=self.headers()).status_code, 200)
        # Stage 2 adds explicit endpoints; Stage 1 resolver remains reusable.
        self.assertIn('/api/teams/{team_id}/projects/{project_id}/tasks/{task_id}/submissions',
                      self.main.app.openapi()['paths'])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = fixtures.test_engine()
        self.addCleanup(fixtures.close_test_engine, self.engine)
        database.create_startup_tables(self.engine)

    def test_startup_does_not_create_new_table_or_change_task_columns(self):
        schema = inspect(self.engine)
        self.assertFalse(schema.has_table('task_submission_targets'))
        self.assertNotIn('project_id', {c['name'] for c in schema.get_columns('tasks')})
        database.create_startup_tables(self.engine)
        self.assertFalse(inspect(self.engine).has_table('task_submission_targets'))

    def test_migration_on_memory_sqlite_preserves_legacy_rows_and_enforces_fks(self):
        # PostgreSQL実DBには接続しない。このDDLの互換部分だけをSQLiteメモリDBで確認。
        if self.engine.dialect.name != 'sqlite':
            self.skipTest('This test only applies DDL to in-memory SQLite')
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO users(id,firebase_uid,email) VALUES (1,'legacy','legacy@example.com')"))
        script = (Path(__file__).parents[1] / 'migrations/005_task_submission_targets.sql').read_text(encoding='utf-8')
        connection = self.engine.raw_connection()
        try:
            connection.driver_connection.executescript(script)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.driver_connection.execute("INSERT INTO task_submission_targets(task_id,project_id,student_id,folder_path,registered_by) VALUES(99,99,1,'task',1)")
            connection.rollback()
            with self.assertRaises(sqlite3.OperationalError):
                connection.driver_connection.executescript(script)
            connection.rollback()
        finally:
            connection.close()
        self.assertTrue(inspect(self.engine).has_table('task_submission_targets'))
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT firebase_uid FROM users WHERE id=1')).scalar_one(), 'legacy')
