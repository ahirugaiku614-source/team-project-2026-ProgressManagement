"""署名済みPython要求の予約・監視・終了確認・後片付け。実Engineは注入されていない。"""

import json
import time
from uuid import uuid4

from app.execution_client import ExecutionReply
from app.schemas import EXECUTION_OUTPUT_MAX_BYTES, ExecutionResponse
from execution_worker.contracts import Busy, Denied, Frame, Unavailable, full_id
from execution_worker.engine import Driver, specification


def output_text(raw):
    """raw byte上限を保ち、壊れたUTF-8は置換する。置換で増えるbytesも再制限する。"""
    return bytes(raw).decode("utf-8", errors="replace").encode("utf-8")[:EXECUTION_OUTPUT_MAX_BYTES].decode("utf-8", errors="ignore")


class Service:
    """別process用workerの中核。同じ専用台帳を使う全呼出しで4枠を共有する。"""

    def __init__(self, registry, authenticator, driver=None, wall_ms=None, monotonic_ms=None):
        """実接続は既定で禁止。時計・Engineをモックに置き換えて障害を再現できる。"""
        self.registry, self.authenticator = registry, authenticator
        self.driver = driver if driver is not None else Driver()
        self.wall_ms = wall_ms or (lambda: time.time_ns() // 1000000)
        self.monotonic_ms = monotonic_ms or (lambda: time.monotonic_ns() // 1000000)

    def handle(self, body, timestamp, nonce, signature, *, trusted_peer=False):
        """通信相手と署名を検証した要求だけを処理する。再送は同一IDの履歴を返す。"""
        try:
            command = self.authenticator.verify(body, timestamp, nonce, signature, self.wall_ms(), trusted_peer)
            labels = {"org.team-progress.kind": "python-execution-v1", "execution_id": str(command.execution_id),
                      "team_id": str(command.owner.team_id), "project_id": str(command.owner.project_id),
                      "user_id": str(command.owner.user_id), "generation": "1", "ownership_nonce": str(uuid4())}
            # 未設定imageなら、予約/副作用に進まず固定拒否。承認済みimageを自動取得しない。
            spec = specification(self.driver.image, labels)
            self.registry.bind(self.driver.profile())
            row, fresh = self.registry.admit(command, nonce, self.wall_ms(), spec)
            if not fresh:
                if row["state"] != "COMPLETED" or row["result"] is None:
                    raise Unavailable()
                result = ExecutionResponse.model_validate(json.loads(row["result"]))
                return ExecutionReply(execution_id=command.execution_id, owner=command.owner, result=result)
            # 満枠で拒否された要求ごとにlockファイルを増やさず、予約済みIDだけをlockする。
            with self.registry.job_lock(command.execution_id):
                return self._run(command, row, int(timestamp))
        except (Denied, Busy):
            raise
        except Exception:
            raise Unavailable() from None

    def _run(self, command, row, issued_ms):
        """create前に意図、start前に完全IDを保存する。失敗時は巻戻し/自動再作成をしない。"""
        try:
            spec = json.loads(row["spec"])
            self._fresh(issued_ms)
            self.driver.ready()
            self._fresh(issued_ms)
            row = self.registry.advance(row, "CREATING")
            cid = self.driver.port.create("python-execution-" + str(command.execution_id), spec)
            if not full_id(cid):
                raise Unavailable()
            row = self.registry.advance(row, "CREATING", container_id=cid)
            observed = self.driver.owned(cid, spec)
            if observed.running:
                raise Unavailable()
            self.driver.port.attach(cid)
            self.driver.owned(cid, spec)
            self._fresh(issued_ms)
            started = self.monotonic_ms()
            self.driver.port.start(cid)
            row = self.registry.advance(row, "RUNNING")
            self.driver.owned(cid, spec)
            if self.monotonic_ms() - started < command.policy.timeout_ms:
                self.driver.port.send(cid, command.code.encode("utf-8"))
            result = self._collect(cid, spec, started, command.policy.timeout_ms)
            # remove前に結果を保存する。削除後のDB障害でも、再実行せず結果を復元できる。
            row = self.registry.advance(row, "CLEANING", result=result.model_dump())
            self.driver.cleanup(cid, spec)
            self.registry.advance(row, "COMPLETED")
            return ExecutionReply(execution_id=command.execution_id, owner=command.owner, result=result)
        except Exception:
            try:
                latest = self.registry.get(command.execution_id)
                if latest["state"] == "RESERVED":
                    # Engineへのcreate要求より前なら、実resourceが存在しないと確定できる。
                    self.registry.advance(latest, "COMPLETED")
                elif latest["state"] != "COMPLETED":
                    self.registry.advance(latest, "UNKNOWN")
            except Exception:
                pass  # DB自体が不通でも元の予約は残る。成功/枠解放を推定しない。
            raise Unavailable() from None

    def _fresh(self, issued_ms):
        """DB待ちや準備中に短期許可が失効したら起動しない。時計逆行も拒否する。"""
        if not 0 <= self.wall_ms() - issued_ms <= 5000:
            raise Unavailable()

    def _collect(self, cid, spec, started, timeout_ms):
        """stdout/stderrを交互に逐次回収し、期限/出力上限で対象全体を停止する。"""
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        eof, reason, exit_code = False, None, None
        while True:
            remaining = timeout_ms - (self.monotonic_ms() - started)
            if remaining <= 0:
                reason = "timeout"
                break
            self.driver.owned(cid, spec)
            frame = None if eof else self.driver.port.read(cid, min(50, remaining), 4096)
            if frame is not None:
                if not isinstance(frame, Frame) or type(frame.data) is not bytes or len(frame.data) > 4096:
                    raise Unavailable()
                if frame.stream == "eof" and frame.data == b"":
                    eof = True
                elif frame.stream in buffers:
                    target = buffers[frame.stream]
                    room = EXECUTION_OUTPUT_MAX_BYTES - len(target)
                    target.extend(frame.data[:room])
                    if len(frame.data) > room:
                        reason = "output_limit"
                        break
                else:
                    raise Unavailable()
            observed = self.driver.owned(cid, spec)
            if eof and not observed.running:
                if observed.exit_code is None:
                    raise Unavailable()
                exit_code = observed.exit_code
                break
        elapsed = max(0, self.monotonic_ms() - started)
        if reason:
            self.driver.stop(cid, spec)
            # 期限/出力ポリシー違反は通常終了コードへ偽装しない。公開契約ではnullとする。
            exit_code = None
            message = ("\n実行時間の上限を超えました。" if reason == "timeout" else "\n出力サイズの上限を超えました。").encode()
            buffers["stderr"] = buffers["stderr"][:EXECUTION_OUTPUT_MAX_BYTES - len(message)] + message
        status = "timeout" if reason == "timeout" else "error" if reason or exit_code != 0 else "completed"
        return ExecutionResponse(status=status, stdout=output_text(buffers["stdout"]), stderr=output_text(buffers["stderr"]),
                                 exit_code=exit_code, execution_time_ms=elapsed)

    def recover(self, execution_id):
        """管理者専用の復旧処理。HTTPには公開せず、記録済み完全ID以外を操作しない。"""
        try:
            with self.registry.job_lock(execution_id):
                try:
                    return self._recover_locked(execution_id)
                except Exception:
                    # lockを保持したままUNKNOWNを記録する。取得失敗した管理者は書き込まない。
                    try:
                        row = self.registry.get(execution_id)
                        if row["state"] != "COMPLETED":
                            self.registry.advance(row, "UNKNOWN")
                    except Exception:
                        pass
                    raise Unavailable() from None
        except Exception:
            raise Unavailable() from None

    def _recover_locked(self, execution_id):
        """排他取得後だけ実施する復旧本体。別所有者・不明IDを推測して採用しない。"""
        row = self.registry.get(execution_id)
        if row["state"] == "COMPLETED":
            return True
        if row["state"] == "RESERVED":
            self.registry.advance(row, "COMPLETED")
            return True
        if row["container_id"] is None:
            if row["state"] != "UNKNOWN":
                self.registry.advance(row, "UNKNOWN")
            return False
        spec = json.loads(row["spec"])
        self.registry.bind(self.driver.profile())
        self.driver.ready()
        if spec["Image"] != self.driver.image:
            raise Unavailable()
        observed = self.driver.owned(row["container_id"], spec, absent=True)
        if observed is not None:
            self.driver.stop(row["container_id"], spec)
        if row["state"] != "UNKNOWN":
            row = self.registry.advance(row, "UNKNOWN")
        row = self.registry.advance(row, "CLEANING")
        self.driver.cleanup(row["container_id"], spec)
        self.registry.advance(row, "COMPLETED")
        return True
