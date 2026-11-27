"""明示注入専用のLinux UDS transport。既定は無効で、providerへ自動登録しない。"""
import os
from pathlib import PurePosixPath
import socket
import stat
import struct
import sys
import time

from app.submission_rpc import reject


def check_endpoint(path, peer_uid):
    """管理側で設定したsocketと親階層を検査する。クライアント要求から呼出先を作らない。

    親はrootまたは収集サービス所有、other書込み不可。管理グループ/ACLの構成は
    導入前の別検証が必要。接続後のSO_PEERCREDとHMACも併用し、パスだけを信用しない。
    """
    current = PurePosixPath('/')
    for part in PurePosixPath(path).parts[1:-1]:
        current /= part
        info = os.lstat(str(current))
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, peer_uid) or info.st_mode & 0o002: reject()
    info = os.lstat(path)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != peer_uid or info.st_mode & 0o007: reject()
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode


class UnixSocketTransport:
    """一接続・一要求・一応答の期限付き通信。自動再接続・再送はしない。

    Windows単体試験ではsocketを偽物へ差し替える。enabled=Falseの既定値では
    lstatもsocket作成も行わない。本クラスの実接続有効化は別途承認を要する。
    """
    def __init__(self, endpoint, expected_peer_uid, *, enabled=False, monotonic=time.monotonic):
        if (type(endpoint) is not str or not endpoint.startswith('/') or '\x00' in endpoint
                or '..' in PurePosixPath(endpoint).parts or str(PurePosixPath(endpoint)) != endpoint
                or len(endpoint.encode('utf-8')) > 107
                or type(expected_peer_uid) is not int or expected_peer_uid <= 0
                or type(enabled) is not bool): reject()
        self.endpoint, self.peer_uid = endpoint, expected_peer_uid
        self.enabled, self.monotonic = enabled, monotonic

    def exchange(self, frame, *, timeout, max_response):
        if not self.enabled or sys.platform != 'linux': reject()
        if (not isinstance(frame, bytes) or not 4 < len(frame) <= 16388
                or type(timeout) not in (int, float) or not 0 < timeout <= 10
                or type(max_response) is not int or not 4 < max_response <= 15 * 1024 * 1024 + 4): reject()
        deadline = self.monotonic() + timeout
        def remaining(sock):
            seconds = deadline - self.monotonic()
            if not 0 < seconds <= timeout: reject()
            sock.settimeout(seconds)
        def read_exact(sock, size):
            chunks = bytearray()
            while len(chunks) < size:
                remaining(sock)
                part = sock.recv(min(65536, size-len(chunks)))
                if not part: reject()  # ヘッダー/本文途中のEOFは成功にしない。
                chunks.extend(part)
            return bytes(chunks)
        try:
            before = check_endpoint(self.endpoint, self.peer_uid)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                remaining(sock); sock.connect(self.endpoint)
                # kernelが返す接続相手UIDを確認する。要求JSON内のUIDは認証に使わない。
                _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != self.peer_uid or check_endpoint(self.endpoint, self.peer_uid) != before: reject()
                remaining(sock); sock.sendall(frame)
                sock.shutdown(socket.SHUT_WR)
                header = read_exact(sock, 4); size = int.from_bytes(header, 'big')
                if not 0 < size <= max_response-4: reject()
                response = header + read_exact(sock, size)
                # サーバーは応答後にcloseする契約。余剰データ/二個目のフレームも拒否する。
                remaining(sock)
                if sock.recv(1): reject()
                if check_endpoint(self.endpoint, self.peer_uid) != before: reject()
                return response
        except Exception:
            # socket例外には内部パス等が含まれるため、固定分類の拒否に置換する。
            reject()
