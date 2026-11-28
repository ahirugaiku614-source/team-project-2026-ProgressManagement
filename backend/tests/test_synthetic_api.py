"""認証以外は製品routeと実SQLiteを接続する。Firebase検証を成功mockで迂回しない。"""
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.synthetic_auth import SyntheticAuth,password_record
from app.synthetic_application import build_synthetic_app
from test_synthetic_auth import guard


class SyntheticAPITests(unittest.TestCase):
    def setUp(self):
        from app import models
        from app.db import database
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        self.addCleanup(self.engine.dispose)
        models.Base.metadata.create_all(self.engine)
        self.db=sessionmaker(bind=self.engine)();self.addCleanup(self.db.close)
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.passwords={'A':'synthetic-api-A-password-123','B':'synthetic-api-B-password-456'}
        accounts={label:{'firebase_uid':'stage10-synthetic-'+label.lower(),'email':label.lower()+'@example.com',
            'name':'合成'+label,**password_record(password)} for label,password in self.passwords.items()}
        from app.synthetic_fixture import seed_database
        self.receipt=seed_database(self.db,accounts,str(uuid4()))
        with patch.object(database,'engine',self.engine):
            routes=importlib.import_module('app.application_routes')
        self.base=routes.app
        self.before=dict(self.base.dependency_overrides)
        self.auth=SyntheticAuth.provision(Path(self.directory.name)/'auth.sqlite',accounts,guard=guard)
        self.app=build_synthetic_app(self.base,self.auth,'https://localhost:28443')
        self.app.dependency_overrides[database.get_db]=lambda:self.db
        self.client=TestClient(self.app,raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def login(self,label='A'):
        response=self.client.post('/api/synthetic/login',headers={'Origin':'https://localhost:28443'},
            json={'account':label,'password':self.passwords[label]})
        self.assertEqual(response.status_code,200)
        return {'Authorization':'Bearer '+response.json()['token']}

    def test_real_product_identity(self):
        response=self.client.get('/api/me',headers=self.login())
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['id'],self.receipt['members']['A']['member_id'])

    def test_unauthenticated_product_request(self):
        self.assertIn(self.client.get('/api/teams').status_code,(401,403))

    def test_uid_impersonation_rejected(self):
        self.assertEqual(self.client.get('/api/me',headers={'Authorization':'Bearer stage10-synthetic-a'}).status_code,401)

    def test_cross_origin_login_rejected(self):
        response=self.client.post('/api/synthetic/login',headers={'Origin':'https://other.invalid'},
            json={'account':'A','password':self.passwords['A']})
        self.assertEqual(response.status_code,401)

    def test_validation_does_not_echo_password(self):
        response=self.client.post('/api/synthetic/login',json={'account':'A','password':self.passwords['A'],'uid':'B'})
        self.assertEqual(response.status_code,422)
        self.assertNotIn(self.passwords['A'],response.text)

    def test_logout_revokes_product_access(self):
        headers=self.login()
        self.assertEqual(self.client.post('/api/synthetic/logout',headers=headers).status_code,200)
        self.assertEqual(self.client.get('/api/me',headers=headers).status_code,401)

    def test_normal_app_not_modified(self):
        self.assertEqual(self.base.dependency_overrides,self.before)

    def test_marker_on_error(self):
        response=self.client.get('/api/me')
        self.assertEqual(response.headers['X-Stage10-Environment'],'synthetic-only')
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_other_members_task_is_forbidden(self):
        team=self.receipt['team_id'];project=self.receipt['project_id'];task=self.receipt['members']['B']['task_id']
        response=self.client.post(f'/api/teams/{team}/projects/{project}/tasks/{task}/submission-permits',
            headers=self.login(),json={'editing_session_id':str(uuid4())})
        self.assertEqual(response.status_code,403)

    def test_membership_revocation_applies_to_product_api(self):
        from app import models
        headers=self.login()
        self.db.query(models.TeamMember).filter_by(team_id=self.receipt['team_id'],
            user_id=self.receipt['members']['A']['member_id']).delete();self.db.commit()
        response=self.client.get(f"/api/teams/{self.receipt['team_id']}/projects",headers=headers)
        self.assertEqual(response.status_code,403)


if __name__=='__main__':unittest.main()
