"""worker内部の固定契約。ユーザー指定Docker引数・hostパスは受け付けない。"""

from dataclasses import dataclass
import hashlib
import json
import re


class Unavailable(Exception):
    """基盤障害・不明状態の固定例外。コード・鍵・内部例外を含めない。"""


class LockBusy(Unavailable):
    """別の実行管理者がOS lockを保持中。孤立ジョブと誤認して状態を変更しない。"""


class StreamDisconnected(Unavailable):
    """通信切断・不完全frame・EOF欠落。正常な両stream終了とは区別する。"""


class Denied(Exception):
    """通信相手・署名・再送nonce・所有者を確認できないときの固定拒否。"""


class Busy(Exception):
    """全体4枠が使用中、またはUNKNOWNによって新規受付が保留されている。"""


def canonical(value):
    """型を保つJSONに正規化する。bool/intの単純比較による設定混同を避ける。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    """コードを台帳に保存せず、同一実行IDの要求すり替えを検出する。"""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def full_id(value):
    """短縮ID・名前・任意パスをコンテナ操作対象にしない。"""
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def image_id(value):
    """tag/latestではなく、ローカルで検証済みのimmutable image IDだけを扱う。"""
    return type(value) is str and value.startswith("sha256:") and full_id(value[7:]) and value[7:] != "0" * 64


@dataclass(frozen=True)
class Observation:
    """engineアダプターが実inspectから正規化すべき情報。現段階はモックだけが発行する。"""

    container_id: str
    spec: dict
    running: bool
    exit_code: int | None


@dataclass(frozen=True)
class Frame:
    """非TTYのstdout/stderr断片。eof以外は最大4096 raw bytesに限定する。"""

    stream: str
    data: bytes = b""


@dataclass(frozen=True)
class AttachAck:
    """start前に非TTYの3方向が接続済みであることを示す合成/将来アダプターの応答。"""

    stdin: bool
    stdout: bool
    stderr: bool


@dataclass(frozen=True)
class SendAck:
    """source全量送信とstdin半closeの確認。部分送信を成功へ読み替えない。"""

    bytes_sent: int
    stdin_closed: bool
