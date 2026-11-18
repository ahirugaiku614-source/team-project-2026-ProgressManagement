"""runscの代わりに合成行だけを更新する試験モック。実Docker/runscを呼ばない。"""

from copy import deepcopy
from uuid import UUID

from app.editor_contracts import EditorBinding
from gvisor_lab.lifecycle import Adapter, MockObservation
from gvisor_lab.policy import Capabilities, Denied, Profile, identities
from gvisor_lab.store import Store


class Clock:
    """期限と失敗を待機なしで検査する仮想時計。"""

    def __init__(self):
        """全プロセスで同じ合成epochから開始する。"""
        self.now = 1000000

    def __call__(self):
        """合成epochミリ秒を返す。OS時計には依存しない。"""
        return self.now


class Members:
    """現在所属を取り消せる試験モック。Firebaseへの接続やユーザー作成はしない。"""

    def __init__(self, bindings):
        """テスト用所有関係を登録する。"""
        self.bindings = {tuple(b.model_dump().values()) for b in bindings}

    def allowed(self, actor, binding, kind):
        """本人かつ現在の合成所属に含まれる要求だけ許可する。"""
        return actor == binding.user_id and tuple(binding.model_dump().values()) in self.bindings


def capabilities(binding, clock):
    """架空digestと強制済みという仮定を返す。物理ホストの合格証跡ではない。"""
    return Capabilities(binding, clock(), "runsc", "systrap", "arm64", "sha256:" + "2" * 64,
                        "sha256:" + "1" * 64, 10000 + binding.project_id * 100 + binding.user_id,
                        True, True, True, True, True, True, True, 2000)


class FakeExecutor:
    """最低限の境界fencing/冪等性を合成行で再現する。実プロセス隔離を証明しない。"""

    def __init__(self, clock):
        """合成データと呼出し履歴を保持する。停止後も本人データを消さない。"""
        self.clock = clock
        self.rows, self.history, self.calls = {}, {}, []
        self.highest = {}
        self.revision, self.jobs, self.dirty, self.save_ok = 1, 0, False, True
        self.lost_reply = False

    def execute(self, ticket, plan):
        """最終境界でも期限・世代・所有者・保存状態を照合してから模擬作用を確定する。"""
        key, volume = identities(ticket.binding)
        if self.clock() >= ticket.lease_ms or self.highest.get(key, 0) > ticket.generation:
            raise Denied()
        if ticket.operation_id in self.history:
            previous, result = self.history[ticket.operation_id]
            if previous != ticket:
                raise Denied()
            return deepcopy(result)
        if self.highest.get(key, 0) == ticket.generation:
            raise Denied()
        row = self.rows.get(key)
        if row and (row["binding"] != ticket.binding or row["volume"] != volume):
            raise Denied()
        if ticket.kind == "START":
            if row is None and ticket.generation > 1:
                raise Denied()  # 既知volumeの消失を新品作成で隠さない。
            if row is None:
                row = {"binding": ticket.binding, "volume": volume, "data": "synthetic-edit-data"}
                self.rows[key] = row
            row["state"] = "RUNNING"
        else:
            if row is None or not self.save_ok or self.jobs != 0 or self.dirty is not False or self.revision != ticket.save_revision:
                raise Denied()
            row["state"] = "STOPPED"
        self.highest[key] = ticket.generation
        result = MockObservation(ticket, ticket.binding, key, volume, row["state"], deepcopy(plan), ticket.runtime_digest,
                                 10000 + ticket.binding.project_id * 100 + ticket.binding.user_id, 1000, True,
                                 ticket.kind == "STOP", ticket.save_nonce, ticket.save_revision)
        self.calls.append(ticket)
        self.history[ticket.operation_id] = (ticket, deepcopy(result))
        if self.lost_reply:
            raise TimeoutError("SYNTHETIC_SECRET")
        return result

    def connect(self, source, destination):
        """外部接続を全拒否するモック。実ネットワークの疎通試験ではない。"""
        raise Denied()


def competing_prepare(path, user, operation_id, barrier, queue):
    """独立Pythonプロセスで同じ一時台帳へ予約し、全体枠の競合を試験する。"""
    binding = EditorBinding(team_id=1, project_id=1, user_id=user)
    clock = Clock()
    adapter = Adapter(Store(path), Members([binding]), clock_ms=clock)
    try:
        barrier.wait(timeout=20)
        adapter.prepare(user, binding, Profile(2, 2048), UUID(operation_id), capabilities(binding, clock))
        queue.put("reserved")
    except Exception as error:
        queue.put(str(error))
