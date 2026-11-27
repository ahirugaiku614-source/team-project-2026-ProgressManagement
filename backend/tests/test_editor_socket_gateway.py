"""実通信なしの継続接続・失効契約。code-server互換性やLinuxの実証ではない。"""
import asyncio
from contextlib import asynccontextmanager
import unittest
from app.editor_socket_gateway import SocketGateway

WORKSPACE='11111111-1111-4111-8111-111111111111'


class Client:
    def __init__(self):
        self.headers={'origin':'https://team.example'}
        self.cookies={'work_editor_'+WORKSPACE:'x'*43};self.scope={'subprotocols':[]}
        self.accepted=False;self.closed=[]
    async def accept(self,**kwargs):self.accepted=True
    async def close(self,code):self.closed.append(code)
    async def receive(self):await asyncio.Event().wait()
    async def send_text(self,value):pass
    async def send_bytes(self,value):pass


class SocketGatewayTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self,check,**options):
        self.connected=[]
        @asynccontextmanager
        async def connect(endpoint,**kwargs):
            self.connected.append((endpoint,kwargs))
            class Upstream:
                subprotocol=None
                def __aiter__(self):return self
                async def __anext__(self):await asyncio.Event().wait()
                async def send(self,value):pass
            yield Upstream()
        return SocketGateway({WORKSPACE:'ws://member-a:8080'},'https://team.example',check,
                             enabled=True,connector=connect,interval=0.01,**options)

    async def test_revocation_closes_existing_connection(self):
        calls=[]
        def check(*args):calls.append(1);return len(calls)<3
        client=Client();await asyncio.wait_for(self.gateway(check).proxy(client,WORKSPACE,'socket'),1)
        self.assertTrue(client.accepted);self.assertIn(1008,client.closed)
        self.assertNotIn('Cookie',self.connected[0][1]['additional_headers'])

    async def test_expired_or_unauthorized_never_connects(self):
        client=Client();await self.gateway(lambda *_:False).proxy(client,WORKSPACE,'socket')
        self.assertFalse(client.accepted);self.assertEqual(self.connected,[])

    async def test_other_workspace_never_connects(self):
        client=Client();await self.gateway(lambda *_:True).proxy(client,'22222222-2222-4222-8222-222222222222','socket')
        self.assertFalse(client.accepted);self.assertEqual(self.connected,[])

    async def test_wrong_origin_never_connects(self):
        client=Client();client.headers['origin']='https://other.example'
        await self.gateway(lambda *_:True).proxy(client,WORKSPACE,'socket');self.assertEqual(self.connected,[])

    async def test_authorization_failure_during_connection_closes(self):
        calls=[]
        def check(*args):
            calls.append(1)
            if len(calls)>2:raise RuntimeError('synthetic outage')
            return True
        client=Client();await asyncio.wait_for(self.gateway(check).proxy(client,WORKSPACE,'socket'),1)
        self.assertTrue(client.accepted);self.assertIn(1008,client.closed)

    async def test_disabled_never_connects(self):
        client=Client();gateway=self.gateway(lambda *_:True);gateway.enabled=False
        await gateway.proxy(client,WORKSPACE,'socket');self.assertEqual(self.connected,[])

    async def test_invalid_path_never_connects(self):
        client=Client();await self.gateway(lambda *_:True).proxy(client,WORKSPACE,'../other')
        self.assertEqual(self.connected,[])

    async def test_no_cookie_never_connects(self):
        client=Client();client.cookies={};await self.gateway(lambda *_:True).proxy(client,WORKSPACE,'socket')
        self.assertEqual(self.connected,[])


import test_work_environment as fixtures
from app import models
from datetime import datetime


class LiveGateContractTests(unittest.IsolatedAsyncioTestCase):
    """製品FastAPIのcookie検証・DB失効と接続監視を結合する。実ネットワークなし。"""
    setUpClass=fixtures.EnvironmentTests.__dict__['setUpClass']
    tearDownClass=fixtures.EnvironmentTests.__dict__['tearDownClass']
    tearDown=fixtures.EnvironmentTests.tearDown
    headers=staticmethod(fixtures.EnvironmentTests.headers)
    decode_token=staticmethod(fixtures.EnvironmentTests.decode_token)
    register=fixtures.EnvironmentTests.register
    capture=fixtures.EnvironmentTests.capture
    start=fixtures.EnvironmentTests.start
    request=fixtures.EnvironmentTests.request
    prepared=fixtures.EnvironmentTests.prepared
    preview=fixtures.EnvironmentTests.preview
    open_session=fixtures.EnvironmentTests.open_session
    def setUp(self):fixtures.EnvironmentTests.setUp(self)

    async def test_database_revocation_disconnects_existing_socket(self):
        b=self.start();sid=self.open_session(b)
        response=self.request('POST','/'+b.id+'/editor-access',{'session_id':sid})
        name='work_editor_'+b.workspace_id;token=response.cookies[name];calls=[]
        def check(workspace,cookie):
            response=self.client.get('/api/editor-gateway/'+workspace,headers={'Cookie':cookie})
            calls.append(response.status_code)
            if len(calls)==2:
                self.db.get(models.WorkEditingSession,sid).revoked_at=datetime.utcnow();self.db.commit()
            return response.status_code==204
        @asynccontextmanager
        async def connector(*args,**kwargs):
            class Upstream:
                subprotocol=None
                def __aiter__(self):return self
                async def __anext__(self):await asyncio.Event().wait()
                async def send(self,value):pass
            yield Upstream()
        gateway=SocketGateway({b.workspace_id:'ws://member-a:8080'},'https://team.example',check,
            enabled=True,connector=connector,interval=0.01)
        client=Client();client.cookies={name:token}
        await asyncio.wait_for(gateway.proxy(client,b.workspace_id,'socket'),3)
        self.assertTrue(client.accepted);self.assertIn(1008,client.closed)
        self.assertEqual(calls[:2],[204,204]);self.assertNotEqual(calls[-1],204)
