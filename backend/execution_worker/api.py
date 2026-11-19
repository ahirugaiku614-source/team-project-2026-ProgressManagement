"""別processで配備するためのworker ASGI境界。TLS/実接続は既定で未構成。"""

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from execution_worker.auth import MAX_BODY_BYTES
from execution_worker.contracts import Busy, Denied


def create_app(service=None, peer_verifier=None):
    """実transport/peer verifierを自動生成しない。テストは合成peerだけを注入する。"""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.post("/internal/execute")
    async def execute(request: Request):
        """署名付きRUN_PYTHONだけを受け付ける。管理exec/任意ID削除APIは公開しない。"""
        if service is None or peer_verifier is None:
            raise HTTPException(status_code=503, detail="実行workerの接続は無効です。")
        try:
            # peer確認はASGI/TLS境界の情報で行う。HTTPヘッダーの自己申告を信用しない。
            if peer_verifier(request.scope) is not True:
                raise Denied()
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_BODY_BYTES:
                    raise Denied()
                body.extend(chunk)
            reply = await run_in_threadpool(service.handle, bytes(body), request.headers.get("x-execution-timestamp"),
                                            request.headers.get("x-execution-nonce"), request.headers.get("x-execution-signature"),
                                            trusted_peer=True)
            return reply.model_dump(mode="json")
        except Denied:
            raise HTTPException(status_code=401, detail="worker認証を確認できません。") from None
        except Busy:
            raise HTTPException(status_code=429, detail="実行枠を予約できません。") from None
        except Exception:
            raise HTTPException(status_code=503, detail="実行workerを利用できません。") from None

    return app


app = create_app()
