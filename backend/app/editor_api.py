"""第7段階の制御API。所有関係を確認するが、実行・公開・接続は常に拒否する。

既存の認可依存関数を引数で受け取り、別のUser/Project解決方法を作らない。
Dockerソケット、ネットワーククライアント、セッションストアは持たない。
保存領域の初期化・権限変更・停止処理も呼ばないため、既存環境へ副作用を与えない。
"""

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app import models
from app.db.database import get_db
from app.editor_access import verify_editor_firebase_token
from app.editor_contracts import EditorBinding, EditorRuntimeUnavailable, require_editor_runtime


class EditorAction(BaseModel):
    """操作本文は不要。User ID、URL、ボリューム、実行引数等の指定を拒否する。"""
    model_config = ConfigDict(extra="forbid")


class EditorRoute(APIRoute):
    """不許可フィールド等の入力値を、検証エラー本文へそのまま再表示しない。"""
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                # トークン・パスワードを誤って本文へ入れた場合も、その値を応答に反射しない。
                raise HTTPException(status_code=422, detail="入力内容を確認してください。") from None

        return safe_handler


def create_editor_router(team_member_resolver, project_resolver):
    router = APIRouter(route_class=EditorRoute)
    prefix = "/api/teams/{team_id}/projects/{project_id}/editor"

    def editor_member(team_id: int, claims: dict = Depends(verify_editor_firebase_token), db: Session = Depends(get_db)):
        # 認証の失効確認を強化しつつ、User/Teamの解決と所属確認は既存コードをそのまま使う。
        return team_member_resolver(team_id=team_id, current_user=claims, db=db)

    def editor_project(team_id: int, project_id: int, user: models.User = Depends(editor_member), db: Session = Depends(get_db)):
        return project_resolver(team_id=team_id, project_id=project_id, user=user, db=db)

    def binding(project: models.Project = Depends(editor_project), user: models.User = Depends(editor_member)):
        # user_idを本文・URLから取らず、認証・DBを通した値だけで所有関係を確定する。
        return EditorBinding(team_id=project.team_id, project_id=project.id, user_id=user.id)

    @router.get(prefix)
    def editor_state(owner: EditorBinding = Depends(binding)):
        # 「RUNNING」と誤認させない。内部環境キー、保存パス、接続先は返さない。
        return {"project_id": owner.project_id, "user_id": owner.user_id,
                "state": "UNAVAILABLE", "launch_enabled": False, "message": "編集環境は準備中です。"}

    @router.post(prefix + "/start")
    @router.post(prefix + "/stop")
    @router.post(prefix + "/sessions")
    def disabled_action(payload: EditorAction | None = None, owner: EditorBinding = Depends(binding)):
        # 本人確認・現在の所属確認を済ませた後でも、基盤未準備なら副作用を起こさない。
        # 特にstopを既存の共通code-serverに転送したり、セッションを先行発行したりしない。
        try:
            require_editor_runtime()
        except EditorRuntimeUnavailable:
            raise HTTPException(status_code=503, detail="編集環境は準備中です。") from None

    @router.websocket(prefix)
    @router.websocket(prefix + "/{rest:path}")
    async def reject_editor_websocket(websocket: WebSocket, rest: str = ""):
        # 未準備の経路では本人かどうかに関係なくacceptしない。
        # これは認証プロキシではなく全拒否。将来は独立TLSオリジンのゲートウェイで
        # Cookie・正確なOrigin・期限・現在所属を検証し、接続中の失効も実装する必要がある。
        await websocket.close(code=1008)

    return router
