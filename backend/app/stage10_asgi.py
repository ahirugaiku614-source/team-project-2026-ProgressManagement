"""承認後の明示的ASGI入口。通常app.mainの既定拒否は変更しない。"""
import os
import sys
import json

path=os.environ.get('WORKSPACE_BACKEND_SETTINGS_FILE')
if not path or sys.platform!='linux' or os.geteuid()==0:
    raise RuntimeError('Approved Linux workspace settings are required')

from app.workspace_bootstrap import private_bytes,install_backend
settings=json.loads(private_bytes(path,65536))
if not isinstance(settings,dict) or set(settings)!={'workers'} or not isinstance(settings['workers'],list):
    raise RuntimeError('Invalid workspace settings schema')

# 既存Firebase・DB認証を再利用する。この入口も実dirty authorityを有効化しない。
from app.main import app,get_project_team_member
install_backend(app,get_project_team_member,settings['workers'],enabled=True)
