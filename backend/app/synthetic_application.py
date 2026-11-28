"""合成専用appを組み立てる。通常appの認証依存・global状態は変更しない。"""
from datetime import datetime
import time

from fastapi import Depends, FastAPI, HTTPException, Request, Security, APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from app.authentication import verify_firebase_token,security
from app.synthetic_auth import SyntheticAuthDenied


def build_synthetic_app(base_app,auth,origin):
    """明示bootstrapまたはテストからだけ呼ぶ。認証以外のrouteは製品を共有する。"""
    from urllib.parse import urlsplit
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or parsed.netloc not in ('127.0.0.1:28443','localhost:28443') or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Fixed isolated TLS origin required')
    app=FastAPI(title='STAGE10 合成実機検証専用',exception_handlers=dict(base_app.exception_handlers))
    app.include_router(base_app.router)
    from app.db.database import get_db
    from app import models

    def principal(request:Request,res:HTTPAuthorizationCredentials=Security(security),db=Depends(get_db)):
        try:
            current=auth.authenticate(res.credentials)
            user=db.query(models.User).filter_by(firebase_uid=current['firebase_uid']).first()
            if user is None or user.email!=current['email']:raise SyntheticAuthDenied()
            request.state.synthetic_digest=current['session_digest']
            return {'uid':current['firebase_uid'],'email':current['email'],'name':current['name']}
        except SyntheticAuthDenied:
            raise HTTPException(401,detail={'code':'SYNTHETIC_AUTH_REQUIRED'}) from None
        except Exception:
            # 台帳・DBを読めない状態を、失効確定や認証成功として返さない。
            raise HTTPException(503,detail={'code':'SYNTHETIC_AUTH_UNAVAILABLE'}) from None

    app.dependency_overrides[verify_firebase_token]=principal

    class AuthRoute(APIRoute):
        def get_route_handler(self):
            original=super().get_route_handler()
            async def safe(request):
                try:return await original(request)
                except RequestValidationError:return JSONResponse(status_code=422,content={'detail':{'code':'INVALID_SYNTHETIC_LOGIN'}})
                except SyntheticAuthDenied:return JSONResponse(status_code=401,content={'detail':{'code':'SYNTHETIC_AUTH_REQUIRED'}})
                except HTTPException:raise
                except Exception:return JSONResponse(status_code=503,content={'detail':{'code':'SYNTHETIC_AUTH_UNAVAILABLE'}})
            return safe
    class Login(BaseModel):
        model_config=ConfigDict(extra='forbid')
        account:Literal['A','B']
        password:str=Field(min_length=20,max_length=256,repr=False)
    router=APIRouter(route_class=AuthRoute)
    @router.post('/api/synthetic/login')
    def login(request:Request,payload:Login):
        if request.headers.get('origin')!=origin:raise SyntheticAuthDenied()
        return auth.login(payload.account,payload.password)

    @router.post('/api/synthetic/logout')
    def logout(current=Depends(principal),res:HTTPAuthorizationCredentials=Security(security)):
        auth.revoke(res.credentials)
        return {'state':'REVOKED','environment':'SYNTHETIC_ONLY'}
    app.include_router(router)

    @app.middleware('http')
    async def synthetic_marker(request,call_next):
        response=await call_next(request)
        response.headers['X-Stage10-Environment']='synthetic-only'
        response.headers['Cache-Control']='no-store'
        return response
    return app


def bound_sessions(db,auth,login_digest=None):
    """編集セッションを合成ログイン台帳へ結合する。cookie gateは同じ結合を照会する。"""
    from uuid import uuid4
    from datetime import timedelta
    from app import models
    from app.work_environment import SnapshotEditingSessions
    from app.submissions import lock_target
    from app.submission_provider import ProviderUnavailable
    class BoundSessions(SnapshotEditingSessions):
        def verify(self,target,session_id,generation):
            evidence=super().verify(target,session_id,generation)
            actor=db.get(models.User,target.student_id)
            try:
                end=auth.verify_binding(session_id,actor.firebase_uid)
                if end<=int(time.time()):raise SyntheticAuthDenied()
            except Exception:raise ProviderUnavailable('Synthetic editing session is unavailable') from None
            return evidence

        def open(self,actor,basis):
            if login_digest is None:raise ProviderUnavailable('Synthetic login is required')
            target=lock_target(db,actor,basis.team_id,basis.project_id,basis.task_id)
            if actor.id!=basis.member_id or basis.state!='ACTIVE':raise ProviderUnavailable('Editing binding unavailable')
            row=models.WorkEditingSession(id=str(uuid4()),basis_id=basis.id,member_id=actor.id,
                capture_policy=self.capture_policy,expires_at=datetime.utcnow()+timedelta(minutes=10))
            db.add(row)
            try:
                db.flush()
                # binding作成前は共通DB検証だけを明示実行。未結合状態を成功と返さない。
                SnapshotEditingSessions.verify(self,target,row.id,basis.source_generation)
                expires=auth.bind(row.id,login_digest,actor.firebase_uid)
                row.expires_at=min(row.expires_at,datetime.utcfromtimestamp(expires))
                self.verify(target,row.id,basis.source_generation)
                db.commit()
            except Exception:
                db.rollback();raise ProviderUnavailable('Synthetic session outcome unavailable') from None
            return {'session_id':row.id,'expires_at':row.expires_at,'capture_policy':self.capture_policy}
    return BoundSessions(db)
