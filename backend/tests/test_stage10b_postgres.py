"""承認済み合成PostgreSQL専用。ランダムschema内で005～010と重複受付を実証する。

WindowsでURLを設定しなければ接続しない。専用DB名のprefixも確認する。
実DB・既存schemaへ移行しない。今回これらのネイティブ試験は未実行。
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import threading
import sys
import tempfile
import unittest
from uuid import uuid4


@unittest.skipUnless(os.environ.get('STAGE10_SYNTHETIC_POSTGRES') == 'approved'
                     and os.environ.get('PROJECT_TEST_DATABASE_URL'),
                     'Requires approved isolated synthetic PostgreSQL')
class MigrationAndConcurrencyTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url
        from app import models
        url = make_url(os.environ['PROJECT_TEST_DATABASE_URL'])
        self.assertEqual(url.get_backend_name(), 'postgresql')
        self.assertTrue((url.database or '').startswith('stage10_synthetic_'))
        self.engine = create_engine(url, pool_size=3)
        self.addCleanup(self.engine.dispose)
        self.schema = 'stage10b_' + uuid4().hex
        self.text = text
        with self.engine.begin() as db:
            db.execute(text('CREATE SCHEMA ' + self.schema))
        self.addCleanup(self.drop_own_schema)
        scripts = sorted((Path(__file__).parents[1] / 'migrations').glob('*.sql'))
        self.scripts = [p for p in scripts if 5 <= int(p.name[:3]) <= 11]
        self.assertEqual([int(p.name[:3]) for p in self.scripts], list(range(5, 12)))
        added = set()
        for script in self.scripts:
            added.update(re.findall(r'CREATE TABLE\s+(\w+)', script.read_text(encoding='utf-8'), re.I))
        with self.engine.begin() as db:
            db.execute(text('SET LOCAL search_path TO ' + self.schema))
            models.Base.metadata.create_all(db, tables=[t for t in models.Base.metadata.sorted_tables if t.name not in added])
            db.execute(text("INSERT INTO users(id,firebase_uid,email,name) VALUES(1,'synthetic-stage10b','member@example.invalid','synthetic')"))
        # SQL自身のBEGIN/COMMITを維持する。search_pathはこの専用接続だけに固定。
        raw = self.engine.raw_connection()
        try:
            with raw.cursor() as cursor:
                cursor.execute('SET search_path TO ' + self.schema)
                cursor.execute("SET lock_timeout TO '3s'")
                cursor.execute("SET statement_timeout TO '8s'")
                raw.commit()
                for script in self.scripts:
                    cursor.execute(script.read_text(encoding='utf-8'))
                    raw.commit()
        finally:
            raw.close()

    def drop_own_schema(self):
        # 自分がこの試験で作ったUUID付きschemaだけを破棄する。
        self.assertRegex(self.schema, r'^stage10b_[0-9a-f]{32}$')
        with self.engine.begin() as db:
            db.execute(self.text('DROP SCHEMA ' + self.schema + ' CASCADE'))

    @unittest.skipUnless(sys.platform=='linux','Requires Linux Git locks and isolated PostgreSQL')
    def test_real_db_git_opposite_order_exits_without_deadlock(self):
        """実DB行ロックと実Gitロックを逆順に保持し、Git側が待たずに拒否することを検証。"""
        from app.project_storage import ProjectStorage,StorageUnavailable
        from app.review_git import ReviewGit
        with tempfile.TemporaryDirectory() as directory:
            storage=ProjectStorage(str(Path(directory)/'storage'));storage.initialize(1,1)
            git_locked=threading.Event();probe=threading.Event();completed=threading.Event();failures=[]
            def git_then_db():
                try:
                    with ReviewGit(storage).session(1):
                        git_locked.set()
                        if not probe.wait(3):raise AssertionError('Test synchronization timeout')
                        with self.engine.begin() as db:
                            db.execute(self.text('SET LOCAL search_path TO '+self.schema))
                            db.execute(self.text("SET LOCAL lock_timeout TO '2s'"))
                            db.execute(self.text('SELECT id FROM users WHERE id=1 FOR UPDATE'))
                    completed.set()
                except Exception as error:failures.append(type(error).__name__)
            thread=threading.Thread(target=git_then_db,daemon=True);thread.start()
            self.assertTrue(git_locked.wait(3))
            try:
                with self.engine.begin() as db:
                    db.execute(self.text('SET LOCAL search_path TO '+self.schema))
                    db.execute(self.text("SET LOCAL lock_timeout TO '2s'"))
                    db.execute(self.text('SELECT id FROM users WHERE id=1 FOR UPDATE'))
                    probe.set()
                    # DBを保持したままGitを待たない。拒否後にDBを解放して相手を進める。
                    with self.assertRaises(StorageUnavailable):
                        with ReviewGit(storage).session(1):pass
            finally:
                probe.set();thread.join(5)
            self.assertFalse(thread.is_alive());self.assertEqual(failures,[]);self.assertTrue(completed.is_set())

    def test_chain_preserves_user_and_adds_initialization_binding(self):
        from sqlalchemy import inspect
        with self.engine.connect() as db:
            db.execute(self.text('SET LOCAL search_path TO ' + self.schema))
            self.assertEqual(db.execute(self.text('SELECT firebase_uid FROM users WHERE id=1')).scalar_one(), 'synthetic-stage10b')
            columns = {c['name']: c for c in inspect(db).get_columns('work_environment_operations', schema=self.schema)}
            self.assertTrue(columns['base_commit']['nullable'])
            self.assertTrue(inspect(db).has_table('task_review_conversions', schema=self.schema))

    def test_parallel_same_request_is_admitted_once(self):
        from sqlalchemy.exc import IntegrityError
        request = str(uuid4()); barrier = threading.Barrier(2)
        def insert():
            try:
                with self.engine.begin() as db:
                    db.execute(self.text('SET LOCAL search_path TO ' + self.schema))
                    db.execute(self.text("SET LOCAL lock_timeout TO '3s'"))
                    db.execute(self.text("SET LOCAL statement_timeout TO '8s'"))
                    barrier.wait(timeout=5)
                    db.execute(self.text("INSERT INTO work_environment_operations(id,request_id,member_id,team_id,project_id,task_id,state) VALUES(:id,:request,1,1,1,1,'PROCESSING')"), {'id': str(uuid4()), 'request': request})
                return 'ADMITTED'
            except IntegrityError:
                return 'DUPLICATE'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: insert(), range(2)))
        self.assertCountEqual(results, ['ADMITTED', 'DUPLICATE'])
        with self.engine.connect() as db:
            db.execute(self.text('SET LOCAL search_path TO ' + self.schema))
            self.assertEqual(db.execute(self.text('SELECT count(*) FROM work_environment_operations WHERE request_id=:request'), {'request': request}).scalar_one(), 1)
