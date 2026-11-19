"""worker専用通信の署名検証。実TLS/mTLSや秘密鍵の取得・配備は行わない。"""

import hashlib
import hmac
import json
import re
from uuid import UUID

from app.execution_client import ExecutionCommand
from execution_worker.contracts import Denied


MAX_BODY_BYTES = 512 * 1024


def unique_pairs(pairs):
    """重複JSON keyを拒否し、署名側と実行側の解釈違いを作らない。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise Denied()
        result[key] = value
    return result


def reject_constant(value):
    """標準JSONでないNaN/Infinityを認証済み要求にも許可しない。"""
    raise Denied()


class Authenticator:
    """peer確認とHMAC-SHA256短期署名を両方要求する。keyは明示注入だけでログに出さない。"""

    def __init__(self, key):
        """実秘密の生成・環境変数からの有効化はしない。テストは合成keyを渡す。"""
        if type(key) is not bytes or len(key) < 32:
            raise Denied()
        self._key = key

    def verify(self, body, timestamp, nonce, signature, now, trusted_peer):
        """5秒以内の署名とbody全体を照合する。nonceの永続一回消費は予約transactionで行う。"""
        try:
            if (trusted_peer is not True or type(body) is not bytes or len(body) > MAX_BODY_BYTES
                    or type(timestamp) is not str or not re.fullmatch(r"\d{1,16}", timestamp)
                    or not 0 <= now - int(timestamp) <= 5000 or str(UUID(nonce)) != nonce
                    or type(signature) is not str or not re.fullmatch(r"[0-9a-f]{64}", signature)):
                raise Denied()
            message = f"v1\n{timestamp}\n{nonce}\n".encode() + hashlib.sha256(body).hexdigest().encode()
            expected = hmac.new(self._key, message, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise Denied()
            json.loads(body, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
            return ExecutionCommand.model_validate_json(body)
        except Exception:
            raise Denied() from None
