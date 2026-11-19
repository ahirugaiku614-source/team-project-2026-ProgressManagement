"""署名済みPython要求の予約・監視・終了確認・後片付け。実Engineは注入されていない。"""

import json
import time
from copy import deepcopy
from contextlib import ExitStack
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.execution_client import ExecutionReply
from app.schemas import EXECUTION_OUTPUT_MAX_BYTES, ExecutionResponse
from execution_worker.contracts import Busy, Denied, LockBusy, StreamDisconnected, Unavailable, canonical, full_id
from execution_worker.engine import Driver, specification


def output_text(raw):
    """raw byte上限を保ち、壊れたUTF-8は置換する。置換で増えるbytesも再制限する。"""
    return bytes(raw).decode("utf-8", errors="replace").encode("utf-8")[:EXECUTION_OUTPUT_MAX_BYTES].decode("utf-8", errors="ignore")


@dataclass
class _Attempt:
    """1実行の所有確認とI/O/停止試行を記録する。並行要求間で状態を共有しない。"""

    spec: dict
    cid: str | None = None
    owned: bool = False
    io_attempted: bool = False
    close_attempted: bool = False
    stop_attempted: bool = False


class Service:
    """別process用workerの中核。同じ専用台帳を使う全呼出しで4枠を共有する。"""

    def __init__(self, registry, authenticator, driver=None, wall_ms=None, monotonic_ms=None):
        """実接続は既定で禁止。時計・Engineをモックに置き換えて障害を再現できる。"""
        self.registry, self.authenticator = registry, authenticator
        self.driver = driver if driver is not None else Driver()
        self.wall_ms = wall_ms or (lambda: time.time_ns() // 1000000)
        self.monotonic_ms = monotonic_ms or (lambda: time.monotonic_ns() // 1000000)
        # constructor時はEngineへ一切触れない。孤立した未完了だけを記録し受付を保留する。
        self.startup_report = self.startup_check()

    def startup_check(self, recover_owned=False):
        """再起動/追加worker起動時の照合。既定は検出のみ、明示復旧でも排他・所有確認必須。

        admissionと同じ固定lockを使い、予約→実行lock取得の隙間を孤立と誤認しない。
        Dockerの独立watchdogではなく、ハングした生存processのlockを奪取する機能もない。
        """
        if type(recover_owned) is not bool:
            raise Unavailable()
        with self.registry.admission_lock():
            return self._scan_unfinished(recover_owned)

    def _scan_unfinished(self, recover_owned=False):
        """admission排他中に実行lockを即時試行し、生存処理と孤立証跡を区別する。"""
        report = {"active": [], "unresolved": [], "recovered": []}
        for snapshot in self.registry.unfinished():
            key = snapshot["execution_id"]
            try:
                with self.registry.job_lock(UUID(key)):
                    row = self.registry.get(UUID(key))
                    if row["state"] == "COMPLETED":
                        continue
                    if recover_owned:
                        try:
                            if self._recover_locked(UUID(key)):
                                report["recovered"].append(key)
                                continue
                        except Exception:
                            pass  # 所有不一致・Engine障害は成功扱いしない。証跡と枠を保持。
                    row = self.registry.get(UUID(key))
                    if row["state"] != "UNKNOWN":
                        self.registry.advance(row, "UNKNOWN")
                    report["unresolved"].append(key)
            except LockBusy:
                report["active"].append(key)  # 他の管理者が処理中。状態を書き換えない。
        return report

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
            with ExitStack() as locks:
                # 新規受付直前にも孤立を検出する。別processが死んだ後の受付継続を防ぐ。
                with self.registry.admission_lock():
                    self._scan_unfinished()
                    row, fresh = self.registry.admit(command, nonce, self.wall_ms(), spec)
                    if fresh:
                        locks.enter_context(self.registry.job_lock(command.execution_id))
                if not fresh:
                    if row["state"] != "COMPLETED" or row["result"] is None:
                        raise Unavailable()
                    result = ExecutionResponse.model_validate(json.loads(row["result"]))
                    return ExecutionReply(execution_id=command.execution_id, owner=command.owner, result=result)
                return self._run(command, row, int(timestamp))
        except (Denied, Busy):
            raise
        except Exception:
            raise Unavailable() from None

    def _run(self, command, row, issued_ms):
        """create前に意図、start前に完全IDを保存する。失敗時は巻戻し/自動再作成をしない。"""
        guard = _Attempt(json.loads(row["spec"]))
        try:
            spec = guard.spec
            self._fresh(issued_ms)
            self.driver.ready()
            self._fresh(issued_ms)
            row = self.registry.advance(row, "CREATING")
            # adapterが入力dictを変更しても、台帳の所有確認基準を変更させない。
            cid = self.driver.port.create("python-execution-" + str(command.execution_id), deepcopy(spec))
            if not full_id(cid):
                raise Unavailable()
            row = self.registry.advance(row, "CREATING", container_id=cid)
            guard.cid = cid
            observed = self.driver.owned(cid, spec)
            if observed.running:
                raise Unavailable()
            guard.owned = True
            guard.io_attempted = True
            attached_at = self.monotonic_ms()
            self.driver.attach(cid, spec, timeout_ms=1000)
            if self.monotonic_ms() - attached_at > 1000:
                raise StreamDisconnected()
            self.driver.owned(cid, spec)
            self._fresh(issued_ms)
            started = self.monotonic_ms()
            self.driver.port.start(cid)
            row = self.registry.advance(row, "RUNNING")
            self.driver.owned(cid, spec)
            if self.monotonic_ms() - started < command.policy.timeout_ms:
                sent_at = self.monotonic_ms()
                send_timeout = min(1000, command.policy.timeout_ms - (sent_at - started))
                if send_timeout > 0:
                    self.driver.send(cid, spec, command.code.encode("utf-8"), timeout_ms=send_timeout)
                    if self.monotonic_ms() - sent_at > send_timeout:
                        raise StreamDisconnected()
            result = self._collect(cid, spec, started, command.policy.timeout_ms, guard)
            # remove前に結果を保存する。削除後のDB障害でも、再実行せず結果を復元できる。
            row = self.registry.advance(row, "CLEANING", result=result.model_dump())
            self._close_io(guard)
            self.driver.cleanup(cid, spec)
            self.registry.advance(row, "COMPLETED")
            return ExecutionReply(execution_id=command.execution_id, owner=command.owner, result=result)
        except Exception:
            self._fail_safely(command.execution_id, guard)
            raise Unavailable() from None

    def _close_io(self, guard):
        """接続開始を試みた実行だけ閉じる。close失敗を正常終了へ読み替えない。"""
        if guard.io_attempted:
            guard.close_attempted = True
            self.driver.close_io(guard.cid, timeout_ms=1000)

    def _fail_safely(self, execution_id, guard):
        """実行lock保持中だけ呼ぶ例外処理。再確認できる本人IDへ一度だけ停止を試みる。

        すでにkill/removeが失敗した要求を無条件に再試行しない。不明状態を先に永続化し、
        停止・stream閉鎖・削除・確定404・DB更新まで揃った場合だけ受付枠を戻す。
        """
        try:
            row = self.registry.get(execution_id)
            previous_state = row["state"]
            if previous_state == "RESERVED":
                self.registry.advance(row, "COMPLETED")  # create前と確定できる。
                return
            if previous_state == "COMPLETED":
                return
            row = self.registry.advance(row, "UNKNOWN")
            if (guard.owned and not guard.stop_attempted and previous_state in ("CREATING", "RUNNING")
                    and row["container_id"] == guard.cid and canonical(json.loads(row["spec"])) == canonical(guard.spec)):
                self.registry.bind(self.driver.profile())
                self.driver.ready()
                guard.stop_attempted = True
                self.driver.stop(guard.cid, guard.spec)  # 毎回最新inspectで所有者を再照合する。
                self._close_io(guard)
                row = self.registry.advance(row, "CLEANING")
                self.driver.cleanup(guard.cid, guard.spec)
                self.registry.advance(row, "COMPLETED")  # 元の失敗応答は503のまま。成功を捏造しない。
            elif guard.io_attempted and not guard.close_attempted:
                self._close_io(guard)
        except Exception:
            try:
                latest = self.registry.get(execution_id)
                if latest["state"] != "COMPLETED":
                    self.registry.advance(latest, "UNKNOWN")
            except Exception:
                pass  # DB不通でも残存予約は解放されない。独立監視による停止保証は未実装。
        finally:
            # killを確認できない場合も、controller側streamの漏れを防ぐ試行は行う。
            # これはcontainer停止の代用ではなく、成功してもUNKNOWNの枠は解放しない。
            if guard.io_attempted and not guard.close_attempted:
                try:
                    self._close_io(guard)
                except Exception:
                    pass

    def _fresh(self, issued_ms):
        """DB待ちや準備中に短期許可が失効したら起動しない。時計逆行も拒否する。"""
        if not 0 <= self.wall_ms() - issued_ms <= 5000:
            raise Unavailable()

    def _collect(self, cid, spec, started, timeout_ms, guard):
        """stdout/stderrを交互に逐次回収し、期限/出力上限で対象全体を停止する。"""
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        eof, reason, exit_code = False, None, None
        last_observed = None
        while True:
            remaining = timeout_ms - (self.monotonic_ms() - started)
            if remaining <= 0:
                if last_observed is not None and not last_observed.running and not eof:
                    raise StreamDisconnected()  # 終了済みでも出力末尾を確認できない。成功/実行timeoutと混同しない。
                reason = "timeout"
                break
            self.driver.owned(cid, spec)
            frame = None if eof else self.driver.read(cid, min(50, remaining), 4096)
            if frame is not None:
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
            last_observed = observed
            if eof and not observed.running:
                if observed.exit_code is None:
                    raise Unavailable()
                exit_code = observed.exit_code
                break
        elapsed = max(0, self.monotonic_ms() - started)
        if reason:
            guard.stop_attempted = True
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
        self.driver.close_io(row["container_id"], timeout_ms=1000)
        if row["state"] != "UNKNOWN":
            row = self.registry.advance(row, "UNKNOWN")
        row = self.registry.advance(row, "CLEANING")
        self.driver.cleanup(row["container_id"], spec)
        self.registry.advance(row, "COMPLETED")
        return True
