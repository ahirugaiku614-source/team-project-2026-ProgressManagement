"""収集専用RPCの契約。実socket接続・待受は実装せず、既定で拒否する。

GitやDBは管理側に残す。このモジュールは承認済み対象の保存済みバイト列だけを
受け渡す。HMAC鍵・本文・例外全文をログへ出す処理は設けない。
"""
from dataclasses import asdict, dataclass, field
import base64
import hashlib
import hmac
import json
import threading
import time
from uuid import UUID, uuid4

from app.submission_scope import SubmissionTarget, normalize_folder_path
from app.submission_collection import excluded
from app.submission_provider import ProviderUnavailable

MAX_REQUEST = 16384
MAX_RESPONSE = 15 * 1024 * 1024
MAX_FILES = 1000
MAX_FILE = 1024 * 1024
MAX_TOTAL = 10 * MAX_FILE


def reject():
    # 入力値や認証情報を例外へ含めない。API側でも固定メッセージだけを返す。
    raise ProviderUnavailable('Internal collection RPC rejected or uncertain')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def strict_json(raw):
    # 標準jsonの「後勝ち」による重複キー受理を禁止する。入れ子も同じ規則で読む。
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: reject()
            result[key] = value
        return result
    try:
        if raw.startswith(b'\xef\xbb\xbf'): reject()
        return json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=pairs,
                          parse_constant=lambda _: reject())
    except (UnicodeError, ValueError, RecursionError):
        reject()


def pack(payload, key, direction, limit):
    # 要求と応答は別鍵・別ドメイン。要求を応答に反射して認証を迂回できない。
    if not isinstance(key, bytes) or len(key) < 32: reject()
    mac = hmac.new(key, direction + canonical(payload), hashlib.sha256).hexdigest()
    body = canonical({'payload': payload, 'mac': mac})
    if len(body) > limit: reject()
    return len(body).to_bytes(4, 'big') + body


def unpack(frame, key, direction, limit):
    # 長さを検査してからJSON解析する。途中切断・余剰フレーム・巨大入力は拒否する。
    if not isinstance(frame, bytes) or not 4 < len(frame) <= limit + 4: reject()
    if int.from_bytes(frame[:4], 'big') != len(frame) - 4: reject()
    obj = strict_json(frame[4:])
    if not isinstance(obj, dict) or set(obj) != {'payload', 'mac'}: reject()
    if not isinstance(key, bytes) or len(key) < 32: reject()
    if (not isinstance(obj['mac'], str) or len(obj['mac']) != 64
            or any(c not in '0123456789abcdef' for c in obj['mac'])): reject()
    expected = hmac.new(key, direction + canonical(obj['payload']), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(obj['mac'], expected): reject()
    return obj['payload']


def uuid_text(value):
    try:
        if type(value) is not str or str(UUID(value)) != value: reject()
    except (ValueError, TypeError, AttributeError): reject()


def target_from(value):
    if not isinstance(value, dict) or set(value) != {'team_id', 'project_id', 'task_id', 'student_id', 'folder_path'}: reject()
    try: return SubmissionTarget(**value)
    except (ValueError, TypeError, UnicodeError): reject()


REQUEST_KEYS = {'version', 'operation', 'submission_id', 'request_id', 'editing_session_id',
                'target', 'source_generation', 'nonce', 'issued_at', 'expires_at',
                'client_service', 'server_service'}


def validate_request(value, now):
    if not isinstance(value, dict): reject()
    version=value.get('version')
    expected=REQUEST_KEYS | ({'authorization'} if version==2 else set())
    if set(value)!=expected:reject()
    if type(version) is not int or version not in (1,2) or value['operation'] != 'COLLECT_SAVED_TASK': reject()
    for field in ('submission_id', 'request_id', 'editing_session_id', 'nonce'): uuid_text(value[field])
    if type(value['source_generation']) is not int or value['source_generation'] <= 0: reject()
    if type(now) is not int or any(type(value[f]) is not int for f in ('issued_at', 'expires_at')): reject()
    if not value['issued_at'] <= now < value['expires_at'] <= value['issued_at'] + 120: reject()
    if value['client_service'] != 'submission-manager' or value['server_service'] != 'task-collector': reject()
    target_from(value['target'])


def encode_files(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES: reject()
    entries = []; total = 0
    for path, entry in sorted(files.items()):
        try: normalize_folder_path(path)
        except (ValueError, TypeError): reject()
        if any(excluded(part) for part in path.split('/')): reject()
        if not isinstance(entry, tuple) or len(entry) != 2 or entry[0] != '100644': reject()
        body = entry[1]
        if not isinstance(body, bytes) or len(body) > MAX_FILE: reject()
        total += len(body)
        if total > MAX_TOTAL: reject()
        entries.append({'path': path, 'mode': '100644', 'data': base64.b64encode(body).decode('ascii')})
    return entries


def decode_files(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES: reject()
    files = {}; total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'path', 'mode', 'data'}: reject()
        path, data = entry['path'], entry['data']
        try: normalize_folder_path(path)
        except (ValueError, TypeError): reject()
        if path in files or any(excluded(p) for p in path.split('/')) or entry['mode'] != '100644': reject()
        if type(data) is not str or len(data) > 4 * ((MAX_FILE + 2) // 3): reject()
        try: body = base64.b64decode(data, validate=True)
        except (ValueError, UnicodeError): reject()
        if len(body) > MAX_FILE or base64.b64encode(body).decode('ascii') != data: reject()
        total += len(body)
        if total > MAX_TOTAL: reject()
        files[path] = ('100644', body)
    # ファイルとディレクトリの衝突（aとa/b）もGit作成前に排除する。
    for path in files:
        parts = path.split('/')
        if any('/'.join(parts[:i]) in files for i in range(1, len(parts))): reject()
    return files


class DeniedRPCTransport:
    """将来のUDS transport差替え境界。実接続へ自動フォールバックしない。"""
    def exchange(self, frame, *, timeout, max_response):
        reject()


class MemoryReplayLedger:
    """偽環境専用。一回受付を直列化するが、再起動後の永続性は保証しない。

    実接続前には永続的・排他的な台帳へ差し替える必要がある。失敗時も消費を戻さない。
    """
    def __init__(self, limit=10000):
        self.used = set(); self.lock = threading.Lock(); self.limit = limit

    def claim(self, key):
        with self.lock:
            if key in self.used or len(self.used) >= self.limit: reject()
            self.used.add(key)


@dataclass(frozen=True)
class RPCKeys:
    # reprでも秘密を露出させない。鍵の配布・ローテーションは本番接続前の別課題。
    request_key: bytes = field(repr=False)
    response_key: bytes = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.request_key, bytes) or not isinstance(self.response_key, bytes)
                or len(self.request_key) < 32 or len(self.response_key) < 32
                or hmac.compare_digest(self.request_key, self.response_key)): reject()


class CollectorRPCServer:
    """待受機能を持たない処理器。テストの偽transportからだけ呼び出す。

    sourcesは管理側が登録した対象と世代。ユーザー入力からrootやURLを組み立てない。
    authorizeは現在の認可・編集セッション・保存状態を独立確認するコールバック。
    """
    def __init__(self, keys, ledger, sources, authorize, clock=lambda: int(time.time())):
        self.keys, self.ledger, self.sources = keys, ledger, dict(sources)
        self.authorize, self.clock = authorize, clock

    def handle(self, frame):
        request = unpack(frame, self.keys.request_key, b'REQUEST\0', MAX_REQUEST)
        validate_request(request, self.clock())
        target = target_from(request['target'])
        source = self.sources.get(target)
        if source is None or source[0] != request['source_generation']: reject()
        if self.authorize(request) is not True: reject()
        # 収集前に消費する。同一IDへの並行要求・失敗後再送は、新nonceでも拒否する。
        self.ledger.claim(request['submission_id'])
        try:
            files = source[1](target, request['editing_session_id'])
            validate_request(request, self.clock())
            if self.authorize(request) is not True: reject()
            result = {'binding': request, 'saved_files_confirmed': True, 'files': encode_files(files)}
            response = pack(result, self.keys.response_key, b'RESPONSE\0', MAX_RESPONSE)
        except Exception:
            # 記録に失敗しても再帰的に同じ記録処理を呼ばない。受付済みIDは消費のまま。
            if hasattr(self.ledger, 'finish'): self.ledger.finish(request['submission_id'], 'UNKNOWN')
            reject()
        if hasattr(self.ledger, 'finish'): self.ledger.finish(request['submission_id'], 'COMPLETE')
        return response


class CollectorRPCClient:
    """管理側の収集呼出しアダプター。戻り値の本文はメモリ内でGit処理へ渡すだけ。

    transportはdeadline以内に必ず返り、受信中にも上限を検査する契約。現在は偽実装のみ。
    HMACはサービス間認証・改ざん検出であり暗号化ではない。実UDSではsocket権限と
    SO_PEERCRED、別ホストでは相互TLSを追加する。本文をネットワークへ平文公開しない。
    """
    def __init__(self, keys, ledger, generation_for, transport=None,
                 clock=lambda: int(time.time()), monotonic=time.monotonic, timeout=5.0, authorization_issuer=None):
        if type(timeout) not in (float, int) or not 0 < timeout <= 10: reject()
        self.keys, self.ledger, self.generation_for = keys, ledger, generation_for
        self.transport = transport if transport is not None else DeniedRPCTransport()
        self.clock, self.monotonic, self.timeout = clock, monotonic, timeout
        self.authorization_issuer=authorization_issuer

    def collect(self, request):
        start = self.monotonic(); now = self.clock()
        payload = {'version': 1, 'operation': 'COLLECT_SAVED_TASK',
            'submission_id': request.submission_id, 'request_id': request.request_id,
            'editing_session_id': request.editing_session_id, 'target': asdict(request.target),
            'source_generation': self.generation_for(request.target), 'nonce': str(uuid4()),
            'issued_at': now, 'expires_at': now + 90,
            'client_service': 'submission-manager', 'server_service': 'task-collector'}
        validate_request(payload, now)
        if self.authorization_issuer is not None:
            # v1は既存の模擬契約用。実収集bootstrapはv2認可検証器だけを受け付ける。
            payload['version']=2
            payload['authorization']=None
            payload['authorization']=self.authorization_issuer.issue(payload)
        frame = pack(payload, self.keys.request_key, b'REQUEST\0', MAX_REQUEST)
        self.ledger.claim(request.submission_id)
        try:
            response = self.transport.exchange(frame, timeout=self.timeout, max_response=MAX_RESPONSE + 4)
            elapsed = self.monotonic() - start
            if not 0 <= elapsed < self.timeout: reject()
            result = unpack(response, self.keys.response_key, b'RESPONSE\0', MAX_RESPONSE)
            if (not isinstance(result, dict) or set(result) != {'binding', 'saved_files_confirmed', 'files'}
                    or canonical(result['binding']) != canonical(payload)
                    or result['saved_files_confirmed'] is not True): reject()
            validate_request(payload, self.clock())
            return decode_files(result['files'])
        except Exception:
            # 不明結果を成功に変換しない。同じ要求を再送したり別IDでやり直したりしない。
            reject()


if __name__ == '__main__':
    # Composeは構成案であり、この入口から実サービスは起動しない。
    raise SystemExit('実UDS待受は未承認・未実装です。実接続は拒否します。')
