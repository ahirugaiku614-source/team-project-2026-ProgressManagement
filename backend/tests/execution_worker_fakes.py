"""worker試験専用の合成Engine。実Docker/VM/学生Pythonプログラムを実行しない。"""

from collections import deque
from copy import deepcopy
import hashlib
import hmac
import os
from threading import Event, Lock
from uuid import UUID, uuid4

from app.editor_contracts import EditorBinding
from app.execution_client import ExecutionCommand
from execution_worker.auth import Authenticator
from execution_worker.contracts import AttachAck, Frame, Observation, SendAck
from execution_worker.engine import Driver, specification
from execution_worker.registry import Registry


IMAGE = "sha256:" + "1" * 64
KEY = b"synthetic-test-only-worker-key-000000"


def command(execution_id=None, user=1, code="synthetic-code"):
    """実行可能性に依存しない合成codeと本人情報を作る。"""
    return ExecutionCommand(execution_id=execution_id or uuid4(), owner=EditorBinding(team_id=1, project_id=2, user_id=user), code=code)


def signed(request, now=1000000, nonce=None):
    """合成keyで署名する。実Firebaseや本番の通信秘密を使わない。"""
    body = request.model_dump_json().encode()
    timestamp, nonce = str(now), str(nonce or uuid4())
    message = f"v1\n{timestamp}\n{nonce}\n".encode() + hashlib.sha256(body).hexdigest().encode()
    return body, timestamp, nonce, hmac.new(KEY, message, hashlib.sha256).hexdigest()


class Clock:
    """待機なしで5秒deadlineを試験する仮想単調時計。署名用wall clockは別に固定する。"""

    def __init__(self):
        """実sleepをせずに経過量を再現する。"""
        self.ms = 0

    def __call__(self):
        """観測のたびに1msを進め、EOF後も停止しない合成processを再現できる。"""
        self.ms += 1
        return self.ms


class FakeEngine:
    """完全ID・固定設定を保った合成コンテナを管理する。OS隔離を実証するものではない。"""

    def __init__(self, clock=None, mode="normal"):
        """故障注入点と操作履歴を作る。未知の既存resourceは削除対象にしない。"""
        self.clock = clock or Clock()
        self.mode = mode
        self.rows, self.events, self.sources = {}, [], []
        self.serial = 0
        self.lock, self.four, self.release, self.entered = Lock(), Event(), Event(), Event()
        self.block = False
        self.fail_create = self.fail_read = self.fail_kill = self.fail_remove = False
        self.keep_running = self.keep_removed = self.kill_lost = False
        self.runsc_verified = True
        self.fail_attach = self.fail_send = self.fail_close = False
        self.start_lost = False
        self.partial_attach = self.partial_send = self.missing_stdin_close = False
        self.late_attach = self.late_send = self.missing_eof = self.eof_running = False
        self.io_calls = []

    def prerequisites(self, image):
        """実hostで確認すべき事項を合成する。NOT_RUNの実機証跡には変換しない。"""
        return {"linux": True, "rootful": True, "runsc_verified": self.runsc_verified, "cgroup_v2": True,
                "cpu_memory_pids_swap": True, "image_id": image, "image_no_volumes": True,
                "image_environment_clean": True, "image_architecture_verified": True, "daemon_id": "lab-daemon-v1"}

    def create(self, name, spec):
        """create応答消失も再現する。返答を失っても裏側に1resourceだけ残す。"""
        with self.lock:
            self.serial += 1
            cid = f"{self.serial:064x}"
            frames = [Frame("stdout", b"Hello\n"), Frame("stderr", b""), Frame("eof")]
            if self.mode == "error":
                frames = [Frame("stderr", b"ValueError: synthetic\n"), Frame("eof")]
            elif self.mode in ("overflow", "stderr_overflow"):
                frames = [Frame("stderr" if self.mode == "stderr_overflow" else "stdout", b"x" * 4096)] * 17 + [Frame("eof")]
            elif self.mode == "bad_utf8":
                frames = [Frame("stdout", b"\xff" * 4096)] * 16 + [Frame("eof")]
            self.rows[cid] = {"spec": deepcopy(spec), "running": False, "exit_code": 0, "frames": deque(frames)}
            self.events.append(("create", cid))
        if self.fail_create:
            raise TimeoutError("SYNTHETIC_SECRET")
        return cid

    def inspect(self, cid):
        """不在はNone、存在時は実態に相当する合成値を返す。"""
        row = self.rows.get(cid)
        if row is None:
            return None
        if self.eof_running:
            self.clock.ms += 50
        return Observation(cid, deepcopy(row["spec"]), row["running"], row["exit_code"])

    def attach(self, cid, timeout_ms):
        """start前に合成stream接続の順序を記録する。"""
        self.events.append(("attach", cid))
        self.io_calls.append(("attach", timeout_ms))
        if self.fail_attach:
            raise ConnectionError("SYNTHETIC_SECRET")
        if self.late_attach:
            self.clock.ms += timeout_ms + 1
        return AttachAck(True, True, not self.partial_attach)

    def start(self, cid):
        """合成状態だけをRUNNINGへ変える。実コードを一切評価しない。"""
        with self.lock:
            self.rows[cid]["running"], self.rows[cid]["exit_code"] = True, None
            self.events.append(("start", cid))
            if sum(row["running"] for row in self.rows.values()) == 4:
                self.four.set()
        if self.start_lost:
            raise TimeoutError("SYNTHETIC_SECRET")

    def send(self, cid, source, timeout_ms):
        """合成stdinを記録するだけ。exec/subprocess等を使わない。"""
        self.io_calls.append(("send", timeout_ms))
        sent = len(source) // 2 if self.partial_send or self.fail_send else len(source)
        self.sources.append(bytes(source[:sent]))
        if self.fail_send:
            raise ConnectionError("SYNTHETIC_SECRET")
        if self.late_send:
            self.clock.ms += timeout_ms + 1
        return SendAck(sent, not self.missing_stdin_close)

    def close(self, cid, timeout_ms):
        """合成local接続の閉鎖を記録する。container停止/削除とは別の操作である。"""
        self.events.append(("close", cid))
        self.io_calls.append(("close", timeout_ms))
        if self.fail_close:
            raise ConnectionError("SYNTHETIC_SECRET")
        return True

    def read(self, cid, timeout_ms, maximum):
        """出力・待機・故障を再現する。読み取りは最大4096bytesの契約を守る。"""
        self.io_calls.append(("read", timeout_ms, maximum))
        if self.fail_read:
            raise ConnectionError("SYNTHETIC_SECRET")
        self.entered.set()
        if self.block and not self.release.wait(10):
            raise RuntimeError("test synchronization timeout")
        if self.mode == "timeout":
            self.clock.ms += timeout_ms
            return None
        row = self.rows[cid]
        if not row["frames"]:
            self.clock.ms += timeout_ms
            return None
        frame = row["frames"].popleft()
        if frame.stream == "eof":
            if not self.eof_running:
                row["running"], row["exit_code"] = False, 7 if self.mode == "error" else 0
            if self.missing_eof:
                return None
        return frame

    def kill(self, cid):
        """指定された合成IDだけを停止する。失敗/未停止も再現する。"""
        self.events.append(("kill", cid))
        if self.fail_kill:
            raise ConnectionError("SYNTHETIC_SECRET")
        if not self.keep_running:
            self.rows[cid]["running"], self.rows[cid]["exit_code"] = False, 137
        if self.kill_lost:
            raise TimeoutError("SYNTHETIC_SECRET")

    def remove(self, cid):
        """停止した合成IDだけを除く。既存hostのresourceを触る処理ではない。"""
        self.events.append(("remove", cid))
        if self.fail_remove:
            raise ConnectionError("SYNTHETIC_SECRET")
        if self.rows[cid]["running"]:
            raise RuntimeError("cannot remove running fixture")
        if not self.keep_removed:
            del self.rows[cid]


def driver(engine):
    """承認済みと仮定する合成image/daemonを渡す。実daemon IDではない。"""
    return Driver(IMAGE, engine, "lab-daemon-v1")


def reserve_process(path, barrier, queue):
    """独立processのSQLite接続から同じ4枠を競合させる。実Engineは呼ばない。"""
    request = command()
    try:
        registry = Registry(path)
        barrier.wait(20)
        registry.admit(request, str(uuid4()), 1000000, specification(IMAGE, {}))
        queue.put("reserved")
    except Exception as error:
        queue.put(type(error).__name__)


def crash_process(path, execution_id):
    """create意図保存直後に自分の試験processを終了し、kernel lock解放を確認する。"""
    request = command(UUID(execution_id))
    registry = Registry(path)
    row, _ = registry.admit(request, str(uuid4()), 1000000, specification(IMAGE, {}))
    with registry.job_lock(request.execution_id):
        registry.advance(row, "CREATING")
        os._exit(23)  # 実Engineは未接続。自分がspawnした一時SQLite試験processだけを終了。
