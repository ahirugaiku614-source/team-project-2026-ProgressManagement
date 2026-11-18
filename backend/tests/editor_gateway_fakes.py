"""合成Firebase本人情報・現在所属・VM証跡を返す通信なしの試験モック。"""

from app.editor_contracts import EditorBinding
from editor_gateway.contracts import Identity, VMRoute
from editor_gateway.gateway import APP_HOST, APP_ORIGIN, Gateway
from editor_gateway.contracts import RequestContext
from editor_gateway.store import TestSessionStore
from vm_manager.contracts import disk_key


class Clock:
    """待機せず任意の期限へ進める試験時計。現実の時刻・ネットワークは参照しない。"""

    def __init__(self):
        self.now = 100000

    def __call__(self):
        return self.now


class Authority:
    """検証済みUIDからだけUserを解決し、削除・失効・障害を試験で注入する。"""

    def __init__(self):
        self.identity = Identity("synthetic-uid", 1000, 1000000)
        self.binding = EditorBinding(team_id=1, project_id=1, user_id=1)
        self.member, self.revoked, self.failure = True, False, False
        self.checks = []

    def _available(self):
        """依存例外に秘密値が入る状況を模擬し、公開例外への反射を検査する。"""
        if self.failure:
            raise OSError("SYNTHETIC_SECRET /server/private https://private.invalid")

    def verify(self, token, *, check_revoked):
        """合成トークンだけを認識する。実Firebase検証は一切呼ばない。"""
        self._available()
        self.checks.append(("verify", check_revoked))
        if token != "synthetic-id-token" or check_revoked is not True or self.revoked:
            raise ValueError("SYNTHETIC_SECRET")
        return self.identity

    def check_session(self, identity, *, check_revoked):
        """UID・auth_time・期限の連続性と、現在の失効状態を返す。"""
        self._available()
        self.checks.append(("check_session", check_revoked))
        return identity == self.identity and not self.revoked and check_revoked is True

    def resolve(self, uid, project_id):
        """User IDを入力から採用せず、合成の現在所属とProject対応を照合する。"""
        self._available()
        if not self.member or uid != self.identity.uid or project_id != self.binding.project_id:
            raise ValueError("SYNTHETIC_SECRET")
        return self.binding


class Directory:
    """管理操作を持たず、固定したVM/disk所有者・現在世代・短期準備証跡だけを返す。"""

    def __init__(self, clock, binding):
        self.clock, self.binding = clock, binding
        self.generation, self.state, self.ready, self.failure = 1, "RUNNING", True, False
        self.disk_owner = binding

    def lookup(self, binding):
        """期限15秒の証跡を返す。停止・隔離・障害・別所有者を試験で注入できる。"""
        if self.failure:
            raise OSError("SYNTHETIC_SECRET")
        return VMRoute(self.binding, self.disk_owner, str(self.binding.environment_key), disk_key(self.binding),
                       self.generation, self.state, self.ready, self.clock() + 15000)


def issue_context():
    """本人アプリからの発行POSTを表す。実HTTP要求ではない。"""
    return RequestContext("POST", APP_HOST, APP_ORIGIN, APP_HOST)


def exchange_context(host):
    """独立サイトへフォームPOSTする際の完全一致Originとcross-site属性を表す。"""
    return RequestContext("POST", host, APP_ORIGIN, host, fetch_site="cross-site")


def editor_context(host, method="GET"):
    """エディタ自身からのHTTP/WS要求を表す。"""
    return RequestContext(method, host, "https://" + host, host)


def concurrent_exchange(path, code, verifier, host, barrier, queue):
    """独立プロセスから同じ試験DBのコードを消費し、一回限りを検査する。"""
    clock, authority = Clock(), Authority()
    gateway = Gateway(TestSessionStore(path), authority, Directory(clock, authority.binding), clock)
    try:
        barrier.wait(timeout=20)
        gateway.exchange(code, verifier, exchange_context(host))
        queue.put("success")
    except Exception as error:
        queue.put(str(error))
