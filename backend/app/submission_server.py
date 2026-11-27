"""明示注入専用のUDS待受。TCP・自動削除・実providerの有効化は行わない。"""
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import time
from app.submission_rpc import MAX_REQUEST, MAX_RESPONSE, reject


def socket_parent(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or len(str(path).encode()) > 107: reject()
    for p in path.parents:
        s=p.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid not in (0,os.geteuid()) or s.st_mode & 0o002: reject()
    s=path.parent.stat()
    if s.st_uid != os.geteuid() or s.st_mode & 0o007: reject()


class UDSServer:
    """単一接続を直列処理し、受付backlogも1に限定する。

    固定endpointは管理側設定だけから渡す。既存socketは所有者が同じでも消さず拒否する。
    正常終了でもsocketは残す。異常終了後の削除は管理者が同一性を確認して別途承認する。
    """
    def __init__(self, endpoint, handler, allowed_uid, allowed_gid, *, enabled=False,
                 timeout=5.0, monotonic=time.monotonic):
        if type(allowed_uid) is not int or type(allowed_gid) is not int or min(allowed_uid,allowed_gid)<=0: reject()
        if type(timeout) not in (int,float) or not 0<timeout<=10: reject()
        self.endpoint=endpoint; self.handler=handler; self.uid=allowed_uid; self.gid=allowed_gid
        self.enabled=enabled; self.timeout=timeout; self.monotonic=monotonic; self.listener=None

    def start(self):
        if self.enabled is not True or sys.platform!='linux': reject()
        socket_parent(self.endpoint)
        if os.path.lexists(self.endpoint): reject()
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            listener.bind(self.endpoint)
            s=os.lstat(self.endpoint)
            if not stat.S_ISSOCK(s.st_mode) or s.st_uid!=os.geteuid(): reject()
            os.chmod(self.endpoint,0o660)
            self.identity=(s.st_dev,s.st_ino)
            listener.listen(1); listener.settimeout(self.timeout); self.listener=listener
        except Exception:
            listener.close(); reject()  # 残ったパスを自動unlinkしない。

    def process_connection(self, connection):
        # 接続元認証はJSON解析より先に行い、未認証相手の収集を絶対に呼ばない。
        deadline=self.monotonic()+self.timeout
        def budget():
            remaining=deadline-self.monotonic()
            if not 0<remaining<=self.timeout: reject()
            connection.settimeout(remaining)
        def read(n):
            out=bytearray()
            while len(out)<n:
                budget(); data=connection.recv(min(n-len(out),65536))
                if not data: reject()
                out.extend(data)
            return bytes(out)
        try:
            _,uid,gid=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if (uid,gid)!=(self.uid,self.gid): reject()
            header=read(4); size=int.from_bytes(header,'big')
            if not 0<size<=MAX_REQUEST: reject()
            frame=header+read(size)
            # クライアントは送信方向をshutdownして、一要求だけであることを示す。
            budget()
            if connection.recv(1): reject()
            budget(); response=self.handler.handle(frame); budget()
            if not isinstance(response,bytes) or len(response)>MAX_RESPONSE+4: reject()
            connection.sendall(response)
        except Exception:
            reject()  # 本文・鍵・パス・生例外は出力しない。

    def serve_once(self):
        if self.listener is None: reject()
        socket_parent(self.endpoint)
        s=os.lstat(self.endpoint)
        if (s.st_dev,s.st_ino)!=self.identity or not stat.S_ISSOCK(s.st_mode) or s.st_uid!=os.geteuid() or s.st_mode & 0o007: reject()
        connection,_=self.listener.accept()
        with connection: self.process_connection(connection)

    def serve(self, stop_requested):
        # 明示bootstrapが渡した終了条件だけを見る。シグナルや他サービスを操作しない。
        from app.submission_provider import ProviderUnavailable
        self.start()
        try:
            while not stop_requested():
                try:self.serve_once()
                except socket.timeout:continue
                except ProviderUnavailable:continue
        finally:self.close()

    def close(self):
        if self.listener is not None: self.listener.close(); self.listener=None


if __name__=='__main__':
    # 設定・鍵・認可/保存セッション検証器の注入なしでは待受を開始しない。
    raise SystemExit('設定と認可検証器の明示注入が必要です。起動を拒否します。')
