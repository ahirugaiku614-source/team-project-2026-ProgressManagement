"""POST /executeの入力・認可・モックworker呼出し。学生コードをAPI内で実行しない。"""

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.editor_access import verify_editor_firebase_token
from app.editor_contracts import EditorBinding
from app.execution_client import ExecutionBusy, ExecutionClient, ExecutionCommand, WorkerUnavailable, get_execution_client
from app.schemas import ExecutionCreate, ExecutionResponse


class ExecutionRoute(APIRoute):
    """実行APIだけの検証エラーを固定文面にし、学生コードや余計なtokenを再表示しない。"""

    def get_route_handler(self):
        """通常のFastAPI処理を保ち、入力検証エラーの本文だけを秘匿する。"""
        original = super().get_route_handler()

        async def safe_handler(request):
            """コードを含むPydanticのraw errorをHTTP応答へ反射させない。"""
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(status_code=422, detail="実行リクエストの入力内容を確認してください。") from None

        return safe_handler


def create_execution_router(team_member_resolver, project_resolver):
    """既存の所属/Project解決関数を注入し、別の権限判定を重複実装しない。"""
    router = APIRouter(route_class=ExecutionRoute)

    def execution_owner(payload: ExecutionCreate, claims: dict = Depends(verify_editor_firebase_token),
                        db: Session = Depends(get_db)):
        """失効も確認したFirebase UIDから本人を確定し、現在の所属とProject対応を照合する。"""
        user = team_member_resolver(team_id=payload.team_id, current_user=claims, db=db)
        project = project_resolver(team_id=payload.team_id, project_id=payload.project_id, user=user, db=db)
        return EditorBinding(team_id=project.team_id, project_id=project.id, user_id=user.id)

    @router.post("/execute", response_model=ExecutionResponse)
    def execute_python(payload: ExecutionCreate, owner: EditorBinding = Depends(execution_owner),
                       client: ExecutionClient = Depends(get_execution_client)):
        """認可済み本人の要求だけを限定契約へ変換する。既定では未接続503となる。"""
        command = ExecutionCommand(execution_id=uuid4(), owner=owner, code=payload.code)
        try:
            return client.execute(command)
        except ExecutionBusy:
            raise HTTPException(status_code=429, detail="実行要求が混み合っています。", headers={"Retry-After": "1"}) from None
        except WorkerUnavailable:
            raise HTTPException(status_code=503, detail="コード実行環境は準備中または利用できません。") from None

    return router
