"""継続中WebSocketの期限・失効を監視する専用gateway。DB資格情報を持たない。"""
import asyncio
import json
import logging
from urllib.parse import urlsplit,quote
import urllib.request
import urllib.error
from uuid import UUID

from fastapi import FastAPI,WebSocket,WebSocketDisconnect


class GatewayDenied(RuntimeError):pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class FixedLeaseCheck:
    """固定FastAPIの読取りgateだけを呼ぶ。URLは利用者の入力から作らない。"""
    def __init__(self,base):
        url=urlsplit(base)
        if url.scheme!='http' or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:raise ValueError('Invalid manager endpoint')
        self.base=base.rstrip('/')
        # proxy環境変数による迂回・リダイレクトを無効にする。
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def __call__(self,workspace,cookie):
        try:
            request=urllib.request.Request(self.base+'/api/editor-gateway/'+workspace,headers={'Cookie':cookie})
            with self.opener.open(request,timeout=0.7) as response:return response.status==204
        except Exception:return False


class SocketGateway:
    def __init__(self,targets,origin,check,*,enabled=False,connector=None,interval=0.5,upstream_cookie_names=()):
        parsed=urlsplit(origin)
        if parsed.scheme!='https' or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:raise ValueError('TLS origin required')
        if type(interval) not in (int,float) or not 0<interval<=1:raise ValueError('Invalid lease interval')
        for workspace,base in targets.items():
            if str(UUID(workspace))!=workspace:raise ValueError('Invalid workspace')
            endpoint=urlsplit(base)
            if endpoint.scheme!='ws' or endpoint.username or endpoint.password or endpoint.path not in ('','/') or endpoint.query or endpoint.fragment:raise ValueError('Invalid editor endpoint')
        if len(set(targets.values()))!=len(targets):raise ValueError('Editor endpoints must be separate')
        for name in upstream_cookie_names:
            # Cookie名はPython変数名ではない。code-serverのハイフンを含む名前を
            # 許可する一方、区切り・改行・管理capabilityの転送は引き続き拒否する。
            import re
            if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',name)
                    or name.startswith('work_editor_')):raise ValueError('Invalid upstream cookie policy')
        self.targets=dict(targets);self.origin=origin.rstrip('/');self.check=check
        self.upstream_cookie_names=tuple(upstream_cookie_names)
        self.enabled,self.connector,self.interval=enabled,connector,interval

    async def allowed(self,workspace,cookie):
        try:return await asyncio.wait_for(asyncio.to_thread(self.check,workspace,cookie),timeout=1.0) is True
        except Exception:return False

    async def proxy(self,client,workspace,rest,query=''):
        if (self.enabled is not True or workspace not in self.targets or client.headers.get('origin')!=self.origin
                or any(part in ('.','..') for part in rest.split('/')) or '\\' in rest
                or any(ord(c)<32 for c in rest+query)):
            await client.close(code=1008);return
        # 固定workspaceに対応するcookie一つだけを認可側へ渡す。他のcookieや
        # Firebase Bearerをcode-serverへ転送しない。
        name='work_editor_'+workspace;token=client.cookies.get(name,'')
        import re
        if not re.fullmatch('[A-Za-z0-9_-]{43}',token):await client.close(code=1008);return
        cookie=name+'='+token
        if not await self.allowed(workspace,cookie):await client.close(code=1008);return
        endpoint=self.targets[workspace].rstrip('/')+'/'+quote(rest,safe='/@:+,;=-._~')+('?' + query if query else '')
        connector=self.connector
        if connector is None:
            # 既存uvicorn[standard]の依存を使用する。利用不能なら接続を拒否する。
            from websockets.asyncio.client import connect
            connector=connect
        offered=client.scope.get('subprotocols',[])
        forwarded={name:client.cookies[name] for name in self.upstream_cookie_names if name in client.cookies}
        if any(len(value)>4096 or any(ord(c)<32 or c in ';\r\n' for c in value) for value in forwarded.values()):
            await client.close(code=1008);return
        headers={'X-Forwarded-Host':urlsplit(self.origin).netloc,'X-Forwarded-Proto':'https'}
        if forwarded:headers['Cookie']='; '.join(name+'='+value for name,value in forwarded.items())
        transport_logger=logging.getLogger('team_editor_proxy.transport')
        transport_logger.disabled=True
        try:
            async with connector(endpoint,origin=self.origin,subprotocols=offered,
                    additional_headers=headers,logger=transport_logger,open_timeout=2,close_timeout=0.3,max_size=1024*1024,max_queue=8,compression=None,proxy=None) as upstream:
                if not await self.allowed(workspace,cookie):raise GatewayDenied()
                await client.accept(subprotocol=getattr(upstream,'subprotocol',None))
                async def outward():
                    while True:
                        message=await client.receive()
                        if message['type']=='websocket.disconnect':return
                        value=message.get('text') if message.get('text') is not None else message.get('bytes')
                        if not isinstance(value,(str,bytes)) or len(value.encode() if isinstance(value,str) else value)>1024*1024:raise GatewayDenied()
                        await upstream.send(value)
                async def inward():
                    async for message in upstream:
                        if isinstance(message,str):await client.send_text(message)
                        else:await client.send_bytes(message)
                async def monitor():
                    while True:
                        await asyncio.sleep(self.interval)
                        if not await self.allowed(workspace,cookie):raise GatewayDenied()
                tasks=[asyncio.create_task(f()) for f in (outward,inward,monitor)]
                try:
                    done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    for task in done:task.result()
                finally:
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
        except Exception:
            # 失効・期限・検証不能・通信断はいずれも接続を継続しない。
            pass
        finally:
            try:await asyncio.wait_for(client.close(code=1008),timeout=0.5)
            except Exception:pass


def create_app(gateway):
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @app.websocket('/code-server/{workspace}/{rest:path}')
    async def socket(client:WebSocket,workspace:str,rest:str):
        await gateway.proxy(client,workspace,rest,client.url.query)
    return app


if __name__=='__main__':
    # 外部公開はしない。operatorが用意した固定設定だけを別bootstrapから読む。
    from app.workspace_bootstrap import socket_gateway_main
    socket_gateway_main()
