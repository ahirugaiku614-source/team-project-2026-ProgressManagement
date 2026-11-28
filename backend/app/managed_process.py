"""管理用固定プログラムを期限付き別プロセスで実行する。利用者コードは渡さない。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from app.submission_provider import ProviderUnavailable

MAX_MESSAGE = 15 * 1024 * 1024


class WorkerUnknown(ProviderUnavailable):
    """結果・終了を確認できない。受付を保持し、自動再実行してはいけない。"""


class ManagedProcess:
    def __init__(self, *, enabled=False, timeout=3.0, program=None, process_factory=subprocess.Popen):
        if type(timeout) not in (int,float) or not 0 < timeout <= 8:raise ValueError('Invalid worker deadline')
        self.enabled=enabled;self.timeout=timeout;self.process_factory=process_factory
        self.failed=False
        # program注入はサーバー側テスト専用。HTTPやRPC payloadから選択しない。
        self.program=Path(program) if program else Path(__file__).with_name('managed_worker.py')

    @staticmethod
    def terminate(process):
        try:
            process.terminate()
            try:process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=0.3)
            return True
        except Exception:return False

    def run(self, job):
        if self.enabled is not True or self.failed:raise WorkerUnknown('Managed worker is disabled or quarantined')
        raw=json.dumps(job,separators=(',',':'),ensure_ascii=True).encode('ascii')
        if len(raw)>MAX_MESSAGE:raise WorkerUnknown('Managed request exceeds limit')
        # DB/Firebase/HMAC/PYTHONPATH等を継承しない。Python自身のロードに必要な
        # Windows変数だけを残す。実ワーカーはさらにLinux Landlockを必須にする。
        env={name:os.environ[name] for name in ('SYSTEMROOT','WINDIR') if name in os.environ}
        started=time.monotonic();process=None;read_done=None
        try:
            process=self.process_factory([sys.executable,'-I','-B',str(self.program)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                env=env,close_fds=True)
            output=[];failures=[];read_done=threading.Event()
            def send():
                try:process.stdin.write(len(raw).to_bytes(4,'big')+raw);process.stdin.close()
                except Exception:failures.append(True)
            def receive():
                try:
                    data=process.stdout.read(MAX_MESSAGE+5)
                    if len(data)>MAX_MESSAGE+4:
                        failures.append(True);self.terminate(process)
                    else:output.append(data)
                except Exception:failures.append(True)
                finally:read_done.set()
            writer=threading.Thread(target=send,daemon=True);reader=threading.Thread(target=receive,daemon=True)
            writer.start();reader.start()
            process.wait(timeout=max(0.001,self.timeout-(time.monotonic()-started)))
            read_done.wait(timeout=max(0,self.timeout-(time.monotonic()-started)))
            if not read_done.is_set() or failures or process.returncode!=0 or time.monotonic()-started>=self.timeout:
                raise WorkerUnknown('Managed worker did not confirm a complete result')
            data=output[0] if output else b''
            if len(data)<4 or int.from_bytes(data[:4],'big')!=len(data)-4:raise WorkerUnknown('Incomplete managed result')
            from app.submission_rpc import strict_json
            return strict_json(data[4:])
        except Exception:
            # 終了・結果不明が発生したrunnerは隔離する。新しい要求IDを使って
            # 孤児ワーカーを増やすことも防ぐ。再起動は管理者の別操作とする。
            self.failed=True
            if process is not None:self.terminate(process)
            raise WorkerUnknown('Managed worker outcome unknown; do not repeat') from None
        finally:
            if process is not None:
                # 終了を確認できない場合、I/Oスレッドも待ち続けない。台帳は消費済み。
                if process.poll() is not None and read_done is not None and read_done.is_set():
                    for stream in (process.stdin,process.stdout):
                        try:stream.close()
                        except Exception:pass
