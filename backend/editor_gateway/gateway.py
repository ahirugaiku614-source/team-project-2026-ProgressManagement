"""本人認証、一回限りコード、Cookieセッションを検証する通信なしのゲートウェイ。

公開FastAPI、HTTP/TLSサーバー、プロキシ、実Firebase/VM接続は実装しない。
依存先は既定で拒否する。テストだけがモックAuthority/Directoryを注入する。
"""

import base64
from dataclasses import asdict
from functools import wraps
import hashlib
import hmac
import json
import re
import secrets
import time

from app.editor_contracts import EditorBinding
from vm_manager.contracts import disk_key
from editor_gateway.contracts import (
    AccessDenied, AccessGrant, ConnectionCode, DisabledAuthority, DisabledDirectory,
    Identity, SessionGrant, VMRoute,
)

APP_HOST = "app.example.com"
APP_ORIGIN = "https://" + APP_HOST
COOKIE_NAME = "__Host-editor"
SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
VERIFIER_PATTERN = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")


def opaque(function):
    """公開メソッドの失敗を固定例外に変え、依存先のトークン入り例外を反射しない。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            raise AccessDenied() from None
    return wrapped


def digest(value):
    """高エントロピーのコード/Cookie/CSRFをSHA-256にして保存する。平文の保管は禁止。"""
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def challenge_for(verifier):
    """ブラウザだけが保持するverifierをS256形式にする。plain方式は用意しない。"""
    if type(verifier) is not str or not VERIFIER_PATTERN.fullmatch(verifier):
        raise AccessDenied()
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def editor_host(binding):
    """接続先ホストは所有関係から固定し、任意URL・Hostを選ばせない。予約ドメインのみ。"""
    return f"{binding.environment_key}.editors.example.net"


def check_context(context, host, *, exchange=False, issue=False, websocket=False):
    """TLS終端情報、完全一致Host/Origin、CSRFに関係する要求条件を共通検査する。"""
    if context.scheme != "https" or context.host != host or context.tls_server_name != host:
        raise AccessDenied()
    if exchange or issue:
        if (context.method != "POST" or context.origin != APP_ORIGIN
                or context.content_type != "application/x-www-form-urlencoded"
                or context.fetch_site != ("same-origin" if issue else "cross-site")):
            raise AccessDenied()
    else:
        if context.fetch_site not in ("same-origin", "none"):
            raise AccessDenied()
        if context.method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            raise AccessDenied()
        # 通常のGETはOriginがないこともある。WSと書込みは正確なOriginを必須にする。
        expected = "https://" + host
        if context.origin not in (None, expected) or ((websocket or context.method not in ("GET", "HEAD")) and context.origin != expected):
            raise AccessDenied()
        if websocket and context.method != "GET":
            raise AccessDenied()


@opaque
def session_cookie(header):
    """Cookieヘッダーから唯一の認証Cookieだけを取り出す。重複や制御文字は拒否する。"""
    if type(header) is not str or len(header) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in header):
        raise AccessDenied()
    values = []
    for item in header.split(";"):
        name, value = item.strip().split("=", 1)
        if name == COOKIE_NAME:
            values.append(value)
    if len(values) != 1 or not SECRET_PATTERN.fullmatch(values[0]):
        raise AccessDenied()
    return values[0]


@opaque
def cookie_header(grant, now):
    """新規セッションをHost限定Cookieとして出力する。Domainや安全属性の上書き口はない。"""
    remaining = (grant.expires_ms - now) // 1000
    if not SECRET_PATTERN.fullmatch(grant.session) or not 1 <= remaining <= 300:
        raise AccessDenied()
    return f"{COOKIE_NAME}={grant.session}; Path=/; Max-Age={remaining}; Secure; HttpOnly; SameSite=Lax"


@opaque
def upstream_headers(headers, *, response=False):
    """最小allowlistだけを中継する。Cookie/Authorization/Forwarded/VM Set-Cookieは渡さない。

    参照実装は実WebSocket Upgradeを扱わない。必要なhop-by-hopヘッダーの生成は将来の
    検証済みアダプターが行う。生ヘッダーや本文をログへ出す機能は設けない。
    """
    allowed = {"content-type", "etag"} if response else {"accept", "content-type", "range", "if-none-match"}
    output, seen = [], set()
    for name, value in headers:
        if (type(name) is not str or type(value) is not str or len(name) > 128 or len(value) > 4096
                or not re.fullmatch(r"[A-Za-z0-9-]+", name) or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise AccessDenied()
        name = name.lower()
        if name in seen:
            raise AccessDenied()
        seen.add(name)
        if name in allowed:
            output.append((name, value))
    # セッションを伴う内容を共有キャッシュへ保存しない。上流のCache-Controlは採用しない。
    if response:
        output.extend((("cache-control", "no-store"), ("referrer-policy", "no-referrer")))
    return tuple(output)


class Gateway:
    """本人限定コードとセッションを発行・照合する参照サービス。通信能力は持たない。"""

    def __init__(self, store, authority=None, directory=None, clock_ms=None):
        """試験専用ストアと検証器を受け取る。既定検証器は必ず拒否する。"""
        self.store = store
        self.authority = authority if authority is not None else DisabledAuthority()
        self.directory = directory if directory is not None else DisabledDirectory()
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1000000)

    def _now(self):
        """時刻は正の整数だけを受理する。永続ストアでも逆行を検知して拒否する。"""
        now = self.clock_ms()
        if type(now) is not int or not 0 < now < 2**62:
            raise AccessDenied()
        return now

    def _identity(self, identity):
        """検証器の返答も無条件に信用せず、本人情報・認証時刻・残存期限を検査する。"""
        now = self._now()
        if (not isinstance(identity, Identity) or type(identity.uid) is not str or not 1 <= len(identity.uid) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in identity.uid)
                or type(identity.auth_time_ms) is not int or not 0 <= identity.auth_time_ms <= now
                or type(identity.token_expires_ms) is not int or identity.token_expires_ms <= now):
            raise AccessDenied()

    def _current(self, identity, project_id, expected_binding=None, generation=None):
        """現在の所属・VM所有者・世代・接続証跡を毎回照会し、過去の認可を流用しない。"""
        self._identity(identity)
        if self.authority.check_session(identity, check_revoked=True) is not True:
            raise AccessDenied()
        binding = self.authority.resolve(identity.uid, project_id)
        if not isinstance(binding, EditorBinding) or binding.project_id != project_id or (expected_binding is not None and binding != expected_binding):
            raise AccessDenied()
        route = self.directory.lookup(binding)
        now = self._now()
        self._identity(identity)  # 依存先の待ち時間中に期限が切れた場合も拒否する。
        if (not isinstance(route, VMRoute) or route.binding != binding or route.disk_owner != binding
                or route.vm_id != str(binding.environment_key) or route.disk_id != disk_key(binding)
                or type(route.generation) is not int or route.generation <= 0
                or (generation is not None and route.generation != generation)
                or route.state != "RUNNING" or route.safe_ready is not True
                or type(route.valid_until_ms) is not int or not now < route.valid_until_ms <= now + 15000):
            raise AccessDenied()
        return binding, route

    @opaque
    def issue(self, id_token, project_id, challenge, context):
        """検証済み本人に、30秒・本人VM世代限定・一回限りの接続許可コードを発行する。"""
        check_context(context, APP_HOST, issue=True)
        if (type(id_token) is not str or not 1 <= len(id_token) <= 8192 or type(project_id) is not int or project_id <= 0
                or type(challenge) is not str or not SECRET_PATTERN.fullmatch(challenge)):
            raise AccessDenied()
        identity = self.authority.verify(id_token, check_revoked=True)
        binding, route = self._current(identity, project_id)
        code = secrets.token_urlsafe(32)
        now = self._now()
        expires = min(now + 30000, identity.token_expires_ms)
        with self.store.transaction(now) as db:
            db.execute("INSERT INTO codes VALUES(?,?,?,?,?,?,?,0)",
                       (digest(code), json.dumps(asdict(identity)), binding.model_dump_json(), route.generation,
                        editor_host(binding), challenge, expires))
        return ConnectionCode(code, editor_host(binding), expires)

    @opaque
    def exchange(self, code, verifier, context):
        """verifierと現在認可を確認し、コード消費と新しいセッションの作成を原子的に行う。"""
        if type(code) is not str or not SECRET_PATTERN.fullmatch(code):
            raise AccessDenied()
        challenge = challenge_for(verifier)
        with self.store.transaction(self._now()) as db:
            row = db.execute("SELECT * FROM codes WHERE digest=?", (digest(code),)).fetchone()
        if row is None or row["used"] or row["expires_ms"] <= self._now() or not hmac.compare_digest(row["challenge"], challenge):
            raise AccessDenied()
        check_context(context, row["host"], exchange=True)
        identity = Identity(**json.loads(row["identity_json"]))
        binding = EditorBinding.model_validate_json(row["binding_json"])
        # 保存済みホストも所有関係から再計算する。台帳の取り違えを正しい接続先と扱わない。
        if row["host"] != editor_host(binding):
            raise AccessDenied()
        _, route = self._current(identity, binding.project_id, binding, row["generation"])
        session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self._now()
        expires = min(now + 300000, identity.token_expires_ms)
        with self.store.transaction(now) as db:
            # 別プロセスが先に消費した場合や照会中の期限切れは、新セッションを作らず終了する。
            changed = db.execute("UPDATE codes SET used=1 WHERE digest=? AND used=0 AND expires_ms>?", (digest(code), now)).rowcount
            if changed != 1:
                raise AccessDenied()
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,0)",
                       (digest(session), digest(csrf), row["identity_json"], row["binding_json"], route.generation, row["host"], expires))
        return SessionGrant(session, csrf, row["host"], expires)

    @opaque
    def authorize(self, session, context, *, csrf=None, websocket=False):
        """HTTP毎回/WS開始前/継続再認可の共通判定。最大15秒の転送許可だけを返す。"""
        if type(session) is not str or not SECRET_PATTERN.fullmatch(session):
            raise AccessDenied()
        with self.store.transaction(self._now()) as db:
            row = db.execute("SELECT * FROM sessions WHERE digest=?", (digest(session),)).fetchone()
        if row is None or row["revoked"] or row["expires_ms"] <= self._now():
            raise AccessDenied()
        check_context(context, row["host"], websocket=websocket)
        if not websocket and context.method not in ("GET", "HEAD"):
            if type(csrf) is not str or not SECRET_PATTERN.fullmatch(csrf) or not hmac.compare_digest(digest(csrf), row["csrf_digest"]):
                raise AccessDenied()
        identity = Identity(**json.loads(row["identity_json"]))
        binding = EditorBinding.model_validate_json(row["binding_json"])
        if row["host"] != editor_host(binding):
            raise AccessDenied()
        _, route = self._current(identity, binding.project_id, binding, row["generation"])
        now = self._now()
        with self.store.transaction(now) as db:
            # ログアウトとの競合も最後に再確認。許可直後の失効は継続許可の短い期限で制限する。
            current = db.execute("SELECT revoked,expires_ms FROM sessions WHERE digest=?", (digest(session),)).fetchone()
            if current is None or current["revoked"] or current["expires_ms"] <= now:
                raise AccessDenied()
        now = self._now()
        if min(row["expires_ms"], route.valid_until_ms) <= now:
            raise AccessDenied()
        return AccessGrant(binding, route.vm_id, route.generation, row["host"], min(now + 15000, row["expires_ms"], route.valid_until_ms))

    @opaque
    def revoke(self, session, context, csrf):
        """本人の有効セッションをCSRF確認後に失効させる。更新・期限延長は行わない。"""
        if context.method != "POST":
            raise AccessDenied()
        self.authorize(session, context, csrf=csrf)
        with self.store.transaction(self._now()) as db:
            db.execute("UPDATE sessions SET revoked=1 WHERE digest=?", (digest(session),))
