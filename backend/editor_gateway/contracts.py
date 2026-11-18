"""本人確認・所属照会・VM読取りの契約。認証やVM管理をクライアントへ委譲しない。"""

from dataclasses import dataclass, field
from typing import Protocol

from app.editor_contracts import EditorBinding


class AccessDenied(Exception):
    """すべての失敗を固定文言にする。依存サービスの例外や資格情報は公開しない。"""

    def __init__(self):
        super().__init__("editor_access_denied")


@dataclass(frozen=True)
class Identity:
    """検証サービスだけが作るFirebase本人情報。UIDと認証時刻をセッション再確認に使う。"""

    uid: str = field(repr=False)
    auth_time_ms: int
    token_expires_ms: int


class Authority(Protocol):
    """将来のmTLS認可サービス。署名/iss/aud/期限/失効/無効化と現在所属を確認する契約。

    verifyはFirebase check_revoked=True相当が必須。resolveはUID→Userと現在の
    TeamMember/Project.team_idからBindingを作る。リクエストのUser IDを採用しない。
    check_sessionは生トークンを保存せず、UID・auth_timeの失効/無効化を照会する。
    このProtocol自体に検証能力はなく、実Firebaseアダプターもまだ存在しない。
    """

    def verify(self, token: str, *, check_revoked: bool) -> Identity:
        """IDトークンの署名・期限・失効等を検証し、成功時だけ本人情報を返す。"""
        ...

    def resolve(self, uid: str, project_id: int) -> EditorBinding:
        """検証済みUIDと現在のDB所属から本人Bindingを解決する。対象外なら拒否する。"""
        ...

    def check_session(self, identity: Identity, *, check_revoked: bool) -> bool:
        """保存した認証時刻に対する現在の失効・無効化を再確認する。通信不能も許可しない。"""
        ...


@dataclass(frozen=True)
class VMRoute:
    """読取り専用のVM接続証跡。RUNNINGだけでは不足し、所有者・準備状態・鮮度を束縛する。"""

    binding: EditorBinding
    disk_owner: EditorBinding
    vm_id: str
    disk_id: str
    generation: int
    state: str
    safe_ready: bool
    valid_until_ms: int


class Directory(Protocol):
    """本人VMの状態だけを照会する境界。起動・停止・XML・exec・任意URLの操作は持たない。

    将来のサービスはguest TLS identity、ACL、健康状態、未完了jobも確認して返す。
    PR #11のレジストリでRUNNINGを読んだだけではsafe_ready=Trueにしてはいけない。
    """

    def lookup(self, binding: EditorBinding) -> VMRoute:
        """認証済み所有関係に対応するVMだけの短期接続証跡を返す。VM操作はしない。"""
        ...


class DisabledAuthority:
    """既定の本人・所属検証器。設定フラグに関係なく拒否し、外部通信を一切行わない。"""

    def verify(self, token, *, check_revoked):
        """既定構成ではトークン検証を実行せず拒否する。"""
        raise AccessDenied()

    def resolve(self, uid, project_id):
        """DBを照会せず拒否し、未検証UIDから所有者を生成しない。"""
        raise AccessDenied()

    def check_session(self, identity, *, check_revoked):
        """実失効照会が未実装であるため、既存セッションも許可しない。"""
        raise AccessDenied()


class DisabledDirectory:
    """既定のVM照会器。実VMや管理レジストリに接続せず、接続可能とは判定しない。"""

    def lookup(self, binding):
        """接続準備済みの証跡を生成せず、未準備として拒否する。"""
        raise AccessDenied()


@dataclass(frozen=True)
class RequestContext:
    """将来のHTTPアダプターが重複・不正ヘッダーを拒否して作る、正規化前提の要求情報。

    scheme/SNIはTLS終端の信頼済み情報から取る。Forwarded/X-Forwarded-*を信用しない。
    参照実装にはHTTPパーサー/TLSがなく、このDTOをクライアント本文から作ってはいけない。
    """

    method: str
    host: str
    origin: str | None
    tls_server_name: str
    scheme: str = "https"
    fetch_site: str = "same-origin"
    content_type: str = "application/x-www-form-urlencoded"


@dataclass(frozen=True)
class ConnectionCode:
    """本人ブラウザへ一度だけ渡す許可コード。reprには秘密値を出さない。"""

    code: str = field(repr=False)
    host: str
    expires_ms: int


@dataclass(frozen=True)
class SessionGrant:
    """Cookie発行アダプターだけに渡す新規セッション。既存Cookieは引き継がない。"""

    session: str = field(repr=False)
    csrf: str = field(repr=False)
    host: str
    expires_ms: int


@dataclass(frozen=True)
class AccessGrant:
    """一回のHTTP転送または最長15秒の継続転送の許可。上流の任意URLは含めない。"""

    binding: EditorBinding
    vm_id: str
    generation: int
    host: str
    deadline_ms: int
