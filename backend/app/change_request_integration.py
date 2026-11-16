"""承認済み提出のfast-forward統合と、Git/DB間の途中失敗からの復旧。

DBのPREPAREDを先に確定し、永続activeジャーナルを作ってからmainを更新する。
その後DBをCOMPLETEDにする。Git更新を取り消す処理はない。
再送は同じ依頼の同じ操作を照合し、確定済みなら同じ結果を返す。
"""

from datetime import datetime
import uuid

from sqlalchemy import text

from app import models
from app.change_request_reviews import ReviewForbidden, ReviewNotFound, verify_snapshot
from app.change_requests import row_manifest
from app.integration_git import (
    IntegrationJournal, locked_main, operation_manifest, verify_initial_base,
    verify_completed_main, sync_main,
)
from app.project_storage import StorageConflict
from app.review_git import ReviewGit


def lock_authorized_request(db, team_id, project_id, request_id, user_id):
    """毎トランザクションで現在の所属・対応・承認を読み直してロックする。

    PREPAREDをコミットするとDBロックは解放されるので、Git更新前にも必ず呼ぶ。
    統合者は提出者でもよいが、承認は従来どおり別メンバーの記録でなければならない。
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
    project = db.query(models.Project).filter_by(id=project_id, team_id=team_id).with_for_update(read=True).first()
    if project is None:
        raise ReviewNotFound()
    member = db.query(models.TeamMember).filter_by(team_id=team_id, user_id=user_id).with_for_update(
        read=True, key_share=True).first()
    if member is None:
        raise ReviewForbidden()
    row = db.query(models.ChangeRequest).filter_by(id=request_id, project_id=project_id).populate_existing().with_for_update(
        of=models.ChangeRequest).first()
    if row is None:
        raise ReviewNotFound()
    approval = db.query(models.ChangeRequestReview).filter_by(change_request_id=request_id).populate_existing().with_for_update(read=True).first()
    if (row.state != "OPEN" or approval is None or approval.decision != "APPROVED"
            or approval.reviewer_id == row.submitted_by):
        raise StorageConflict("Only an open approved request can be integrated")
    return row


def integration_result(row):
    """commit後にORMの暗黙SELECTを起こさず、確定した値だけをレスポンスへ渡す。"""
    return {name: getattr(row, name) for name in (
        "change_request_id", "project_id", "operation_id", "integrated_by", "status",
        "prepared_at", "integrated_at", "main_before", "main_after",
    )}


def check_record(record, row):
    """DBの操作が別の提出に結び替えられていないことを確認する。"""
    if (record.project_id != row.project_id or record.change_request_id != row.id
            or record.main_after != row.snapshot_commit
            or record.main_before not in (None, row.base_commit)):
        raise StorageConflict("Integration record is inconsistent; operator inspection required")


def reconcile_active(db, journal, project_id, request_id):
    """他の未確定操作・DBに対応しないジャーナルがあれば次の更新を止める。

    DB確定後でもこの段階では退避しない。スナップショット・現在mainを検査してから退避する。
    DBを古いバックアップへ戻してactiveだけが残った場合も、推測で続行しない。
    """
    active = journal.read()
    if active is None:
        return
    record = db.query(models.ChangeRequestIntegration).filter_by(
        project_id=project_id, operation_id=active["operation_id"],
    ).populate_existing().with_for_update().first()
    if record is None or active != operation_manifest(record):
        raise StorageConflict("Unmatched integration journal; operator inspection required")
    if record.change_request_id != request_id:
        raise StorageConflict("Another integration needs recovery first")


def integrate_change_request(db, storage, team_id, project_id, request_id, user_id):
    """ロック下で一件を準備→公開→確定し、再送なら同じ操作を再開する。

    複数Projectで独立に動作できる。同一Projectは既存flockで直列化し、
    再起動をまたぐ未確定処理はDBの部分一意索引とactiveで検知する。
    """
    try:
        with ReviewGit(storage).session(project_id) as review:
            row = lock_authorized_request(db, team_id, project_id, request_id, user_id)
            with review.new_directory(review.project, "integration-journal") as directory:
                journal = IntegrationJournal(review, directory)
                reconcile_active(db, journal, project_id, request_id)
                record = db.query(models.ChangeRequestIntegration).filter_by(
                    change_request_id=request_id).populate_existing().with_for_update().first()
                pending = db.query(models.ChangeRequestIntegration).filter_by(
                    project_id=project_id, status="PREPARED").first()
                if pending is not None and pending.change_request_id != request_id:
                    raise StorageConflict("Another integration needs recovery first")
                if record is not None:
                    check_record(record, row)

                with review.locked_review_refs(row_manifest(row)["ref_key"]):
                    verify_snapshot(review, row)
                    if record is not None and record.main_before is None:
                        # 再送でも初回用baseを必ず照合する。DBのNULL beforeだけを初回の証拠にしない。
                        verify_initial_base(review, row.base_commit)
                    if record is not None and record.status == "COMPLETED":
                        # 後続統合でmainが進んでも、完了済み操作の再送は履歴上の同じ結果を返す。
                        # ただし永続記録の欠落は隠さない。mainを過去の値へ戻す処理はない。
                        with locked_main(review):
                            tip = verify_completed_main(review, record)
                            sync_main(review, tip)
                            if journal.read() is not None:
                                journal.sync_active(record)
                                journal.archive(record)
                            journal.verify_archive(record)
                            result = integration_result(record)
                            db.rollback()  # 読み取りで取得したDBロックを解放する。
                            return result

                    with locked_main(review) as publish:
                        current = review.read_ref(("heads", "main"))
                        if record is None:
                            if current is None:
                                # 既に統合履歴があるのにmainが消えていたら初回と誤認しない。
                                if db.query(models.ChangeRequestIntegration).filter_by(project_id=project_id).first():
                                    raise StorageConflict("Main is missing after an earlier integration")
                                verify_initial_base(review, row.base_commit)
                            elif current != row.base_commit:
                                raise StorageConflict("Main differs from the submitted baseline")
                            record = models.ChangeRequestIntegration(
                                change_request_id=row.id, project_id=project_id, operation_id=str(uuid.uuid4()),
                                integrated_by=user_id, status="PREPARED", prepared_at=datetime.utcnow(),
                                main_before=current, main_after=row.snapshot_commit,
                            )
                            db.add(record)
                            # Gitより先に永続化する。このcommitが失敗すればGitを一切更新しない。
                            db.commit()
                            # DBコミット後に権限や提出状態が変わる可能性を考慮して再ロックする。
                            row = lock_authorized_request(db, team_id, project_id, request_id, user_id)
                            record = db.query(models.ChangeRequestIntegration).filter_by(
                                change_request_id=request_id).populate_existing().with_for_update().one()
                            check_record(record, row)
                            if record.status != "PREPARED":
                                raise StorageConflict("Prepared integration changed unexpectedly")
                            verify_snapshot(review, row)
                        elif record.integrated_by != user_id:
                            # 途中再開の操作主体を勝手に付け替えない。完了済みの閲覧・再送は許可する。
                            raise StorageConflict("Prepared integration must be resumed by its original member")

                        # DBだけが古いPREPAREDに戻った場合、完了記録を無視して再公開しない。
                        # 同じ操作IDのアーカイブがある時点で自動再開の根拠が矛盾している。
                        if journal.read(record.operation_id + ".json") is not None:
                            raise StorageConflict("Prepared operation already has an archive; operator inspection required")
                        if record.main_before is None and db.query(models.ChangeRequestIntegration).filter(
                            models.ChangeRequestIntegration.project_id == project_id,
                            models.ChangeRequestIntegration.change_request_id != request_id,
                        ).first():
                            raise StorageConflict("Initial prepared operation conflicts with project history")
                        active = journal.read()
                        if active is None:
                            # DB準備の直後に停止した場合のみ記録を作り直せる。
                            # mainが既に変わっていれば、失われたjournalを推測で復元しない。
                            if current != record.main_before:
                                raise StorageConflict("Missing journal after main changed; operator inspection required")
                            journal.prepare(record)
                        elif active != operation_manifest(record):
                            raise StorageConflict("Integration journal and database disagree")
                        journal.sync_active(record)

                        if current == record.main_before:
                            # 初回・未更新の再試行どちらも、正式参照の直前にCASで現在値を再確認する。
                            publish(record.main_before, record.main_after)
                        elif current != record.main_after:
                            raise StorageConflict("Main cannot prove this operation; operator inspection required")
                        # current==main_afterならGit更新後に停止した操作。参照を書き直さずDBだけを確定。
                        # 前回rename直後のfsyncが失敗した可能性があるため、値が同じでも再同期する。
                        sync_main(review, record.main_after)
                        record.status = "COMPLETED"
                        record.integrated_at = max(datetime.utcnow(), record.prepared_at)
                        result = integration_result(record)
                        db.commit()
                        journal.archive(record)
                        return result
    except Exception:
        # PREPAREDを確定した後は、その記録とjournalを残して再送に委ねる。
        # mainやレビュー参照を巻き戻す補償操作は行わない。
        db.rollback()
        raise
