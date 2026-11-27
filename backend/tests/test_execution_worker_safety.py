"""追加レビュー：作成結果不明、遅延応答、台帳障害、OS排他を実Engineなしで検証する。"""

import os
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_execution_worker as base
from execution_worker.contracts import Busy, LockBusy, Unavailable
from execution_worker.registry import Registry
from execution_worker.engine import specification
from execution_worker_fakes import IMAGE, command


class WorkerSafetyTests(unittest.TestCase):
    """全試験に一時SQLiteと合成コンテナを使い、本物のDockerへ接続しない。"""

    setUp = base.WorkerTests.setUp
    make_service = base.WorkerTests.make_service
    run_job = base.WorkerTests.run_job
    state = base.WorkerTests.state

    def assert_unknown_without_id(self, request):
        """ID不明の証跡が再起動・再送で消えず、新規受付も再作成も止まることを確認する。"""
        row = self.registry.get(request.execution_id)
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertIsNone(row["container_id"])
        before = list(self.engine.events)
        restarted = self.make_service()
        self.assertFalse(restarted.recover(request.execution_id))
        with self.assertRaises(Unavailable):
            self.run_job(request, restarted)
        with self.assertRaises(Busy):
            self.run_job(service=restarted)
        self.assertEqual(self.engine.events, before)
        self.assertFalse(any(action in ("start", "kill", "remove") for action, _ in before))

    def test_create_effect_without_response_requires_manual_reconciliation(self):
        """create副作用だけ成功してID応答を失っても、名前やlabelで推測削除しない。"""
        self.engine.fail_create = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(len(self.engine.rows), 1)
        self.assert_unknown_without_id(request)

    def test_id_persistence_failure_never_starts_or_adopts_the_unrecorded_id(self):
        """完全IDを受信しても保存が失敗したら起動しない。自動復旧用IDを捏造しない。"""
        with self.registry.connection() as db:
            db.execute("CREATE TRIGGER reject_cid BEFORE UPDATE OF container_id ON jobs "
                       "WHEN NEW.container_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(len(self.engine.rows), 1)
        self.assert_unknown_without_id(request)

    def test_late_eof_is_timeout_instead_of_success(self):
        """5秒経過後の正常EOFを、期限内に完了したという成功記録へ変換しない。"""
        original = self.engine.read
        def delayed(cid, timeout_ms, maximum):
            """実待機せずEOF到着だけを6秒遅らせる。"""
            frame = original(cid, timeout_ms, maximum)
            if frame is not None and frame.stream == "eof":
                self.clock.ms += 6000
            return frame
        with patch.object(self.engine, "read", side_effect=delayed):
            reply = self.run_job()
        self.assertEqual(reply.result.status, "timeout")
        self.assertIsNone(reply.result.exit_code)
        self.assertFalse(self.engine.rows)

    def test_late_final_inspect_cannot_turn_timeout_into_success(self):
        """終了確認inspectの遅延も実行期限へ算入し、成功と誤認しない。"""
        original = self.engine.inspect
        def delayed(cid):
            """EOF取得後の終了確認だけを遅延させる。"""
            observed = original(cid)
            if observed and not observed.running and any(name == "start" for name, _ in self.engine.events):
                self.clock.ms += 6000
            return observed
        with patch.object(self.engine, "inspect", side_effect=delayed):
            reply = self.run_job()
        self.assertEqual(reply.result.status, "timeout")
        self.assertFalse(self.engine.rows)

    def test_slow_start_never_sends_source_after_execution_deadline(self):
        """start応答が遅延して期限超過した場合、コードを追加送信せず停止確認へ進む。"""
        original = self.engine.start
        def delayed(cid):
            """開始の副作用と遅い応答を合成する。"""
            original(cid)
            self.clock.ms += 6000
        with patch.object(self.engine, "start", side_effect=delayed):
            reply = self.run_job()
        self.assertEqual(reply.result.status, "timeout")
        self.assertFalse(self.engine.sources)
        self.assertFalse(self.engine.rows)

    def test_cleanup_reply_loss_keeps_unknown_until_exact_id_is_rechecked(self):
        """remove副作用後の応答消失では枠を保持し、完全IDの確定不在でだけ復旧する。"""
        original = self.engine.remove
        def lost(cid):
            """削除自体は合成Engineで成功し、その返答を失わせる。"""
            original(cid)
            raise TimeoutError("synthetic")
        request = command()
        with patch.object(self.engine, "remove", side_effect=lost), self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "UNKNOWN")
        self.assertIsNotNone(self.registry.get(request.execution_id)["container_id"])
        self.assertTrue(self.service.recover(request.execution_id))
        self.assertEqual(self.state(request), "COMPLETED")

    def test_unknown_update_failure_keeps_creation_evidence_and_blocks_restart(self):
        """UNKNOWN更新自体が失敗してもCREATINGを残し、再起動照合で受付を止める。"""
        with self.registry.connection() as db:
            db.execute("CREATE TRIGGER reject_unknown BEFORE UPDATE OF state ON jobs "
                       "WHEN NEW.state='UNKNOWN' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        self.engine.fail_create = True
        request = command()
        with self.assertRaises(Unavailable):
            self.run_job(request)
        self.assertEqual(self.state(request), "CREATING")
        with self.assertRaises(Unavailable):
            self.make_service()
        self.assertEqual(len(self.engine.events), 1)

    def test_reservation_db_failure_rolls_back_nonce_and_has_no_engine_effect(self):
        """予約INSERT失敗で4枠やnonceを部分確定せず、Engineを一度も呼ばない。"""
        with self.registry.connection() as db:
            db.execute("CREATE TRIGGER reject_job BEFORE INSERT ON jobs "
                       "BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(Unavailable):
            self.run_job()
        self.assertEqual(self.registry.capacity_report()["jobs"], 0)
        self.assertEqual(self.registry.capacity_report()["nonces"], 0)
        self.assertFalse(self.engine.events)

    def test_lock_contention_never_changes_active_job(self):
        """別接続から実行lockを奪えず、競合を孤立・UNKNOWNと誤判定しない。"""
        request = command()
        self.registry.admit(request, str(uuid4()), 1000000, specification(IMAGE, {}))
        with self.registry.job_lock(request.execution_id):
            with self.assertRaises(LockBusy), Registry(self.path).job_lock(request.execution_id):
                self.fail("競合lockを取得してはいけない")
            self.assertEqual(self.make_service().startup_report["active"], [str(request.execution_id)])
        self.assertEqual(self.state(request), "RESERVED")

    @unittest.skipUnless(os.name == "nt", "Windows byte lockの解放失敗試験")
    def test_windows_unlock_failure_still_closes_descriptor(self):
        """Windowsでもunlock例外の後にFDが残らず、同じlockを再取得できる。"""
        import msvcrt
        original = msvcrt.locking
        def broken_unlock(fd, mode, count):
            """取得は実OSで行い、明示unlockだけ失敗させる。"""
            if mode == msvcrt.LK_UNLCK:
                raise OSError("synthetic unlock failure")
            return original(fd, mode, count)
        with patch("msvcrt.locking", side_effect=broken_unlock), self.assertRaises(OSError):
            with self.registry.admission_lock():
                pass
        with self.registry.admission_lock():
            self.assertTrue(self.path.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux fcntl実経路はWindowsではNOT_RUN")
    def test_linux_unlock_failure_still_closes_descriptor_and_retains_evidence(self):
        """fcntl unlock例外でもFDを閉じ、次の管理者が同じinodeをロックできる。"""
        import fcntl
        original = fcntl.flock
        def broken_unlock(fd, flags):
            """取得は本物のfcntlを使い、明示解放だけを失敗させる。"""
            if flags == fcntl.LOCK_UN:
                raise OSError("synthetic unlock failure")
            return original(fd, flags)
        with patch("fcntl.flock", side_effect=broken_unlock), self.assertRaises(OSError):
            with self.registry.admission_lock():
                pass
        with self.registry.admission_lock():
            self.assertTrue(self.path.with_name(self.path.name + ".admission.lock").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux fcntl複数プロセス試験はNOT_RUN")
    def test_linux_eight_processes_reserve_only_four_slots(self):
        """Ubuntu用：独立SQLite接続8個が競合しても4枠だけを予約する。"""
        base.WorkerTests.test_multiprocess_atomic_four_reservations_survive_reopen(self)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux終了時のfcntl解放試験はNOT_RUN")
    def test_linux_process_death_releases_lock_without_releasing_slot(self):
        """Ubuntu用：自分の合成子process終了後も不明ジョブの予約を保持する。"""
        base.WorkerTests.test_process_death_releases_lock_but_not_uncertain_reservation(self)
