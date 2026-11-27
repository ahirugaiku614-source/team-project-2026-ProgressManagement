"""現構成では独立したdirty真正性を証明できないため、実収集を拒否する。"""
from app.work_environment import DeniedAuthority


class IndependentEditorAuthority(DeniedAuthority):
    """拡張をreadonly配置しただけでは、同じUIDの利用者プロセスから独立しない。

    機能を有効化する設定値は用意しない。管理側PID/IPC、全編集経路、debug・
    拡張・terminal等の権限分離を設計し、証拠源の実証後に実装を置き換える必要がある。
    初期化やgatewayの単体成功を、保存状態の証明へ転用してはいけない。
    """
    reason='EDITOR_TRUST_UNAVAILABLE'
