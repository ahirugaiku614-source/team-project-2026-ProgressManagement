"""通常のFirebase入口。合成認証への切替・fallbackは持たない。"""
# 通常起動は従来どおりFirebase credentialの初期化を要求する。
from app.firebase import verify_firebase_token as _firebase_startup
from app import application_routes as _routes

# 既存のapp.main公開関数・依存関数の参照を維持する。API定義は一か所だけ。
globals().update({name: value for name, value in vars(_routes).items() if not name.startswith('__')})
from app.editor_api import create_editor_router
app.include_router(create_editor_router(get_project_team_member,get_accessible_project))
from app.execution_api import create_execution_router
app.include_router(create_execution_router(get_project_team_member,get_accessible_project))
