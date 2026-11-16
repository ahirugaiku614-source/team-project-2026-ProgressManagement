"""承認・差し戻しの判定を、一件だけ確定するための処理。

このモジュールはGitのmainへ統合しない。既存の提出記録・参照を検証した後、
別テーブルへ判定をINSERTする。処理途中の失敗はDBをロールバックして上位へ伝える。
"""

from datetime import datetime
import hashlib

from sqlalchemy import text

from app import models
from app.change_requests import row_manifest
from app.project_storage import StorageConflict, StorageUnavailable
from app.review_git import ReviewGit, ReviewLimit, MAX_FILES, MAX_TOTAL_BYTES, valid_path


class ReviewForbidden(Exception):
    """認証済みでも、提出者本人や所属を失ったユーザーは判定できない。"""


class ReviewNotFound(Exception):
    """指定Projectに属する依頼が存在しない。別Projectの存在は知らせない。"""


def verified_object(review, kind, oid):
    """Gitオブジェクトを読み取り、格納された内容が指定IDと一致するか確かめる。

    hash-object -wやupdate-refは実行しない。SHA-1は既存Gitの識別子の照合用であり、
    ユーザー認証の代わりにはしない。壊れた内容を判定対象にしないための追加検査。
    """
    review.oid(oid)
    content = review.blob(oid) if kind == "blob" else review.run("cat-file", kind, oid)
    header = f"{kind} {len(content)}\0".encode("ascii")
    actual = hashlib.sha1(header + content, usedforsecurity=False).hexdigest()
    if actual != oid:
        raise StorageConflict("Review object content does not match its identifier")
    return content


def verify_snapshot(review, row):
    """DB、復旧用JSON、loose/packed参照、commit/tree/blobを読み取りだけで照合する。

    判定時に欠落を自動修復すると、検査対象を作り替えることになってしまう。
    欠けていれば判定を中止し、従来の提出再試行・バックアップ復旧へ委ねる。
    """
    manifest = row_manifest(row)
    saved = review.manifest(manifest["ref_key"])
    if saved is None:
        raise StorageUnavailable("Review journal is missing")
    if saved != manifest:
        raise StorageConflict("Review journal and database do not match")
    review.verify_refs(manifest)

    # スナップショットだけでなく、差分の比較元も同じ方法で確認する。
    # baseより前の全履歴を走査するのではなく、両コミットの提出内容を対象とする。
    checked = set()
    for commit in (row.base_commit, row.snapshot_commit):
        content = verified_object(review, "commit", commit)
        headers = content.split(b"\n\n", 1)[0].splitlines()
        roots = [line[5:].decode("ascii") for line in headers if line.startswith(b"tree ")]
        if len(roots) != 1:
            raise StorageConflict("Review commit tree is invalid")
        if commit == row.snapshot_commit:
            parents = [line[7:].decode("ascii") for line in headers if line.startswith(b"parent ")]
            if parents != [row.base_commit]:
                raise StorageConflict("Review snapshot parent does not match its baseline")
        verified_object(review, "tree", roots[0])
        files = review.entries(commit)
        total = 0
        for _, oid in files.values():
            # 各ファイルと総量は提出APIと同じ上限で検査する。
            # 同じblobでも比較元・変更後それぞれの合計量には加算する。
            size = int(review.run("cat-file", "-s", oid))
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ReviewLimit("Review snapshot exceeds the validation limit")
            if oid not in checked:
                verified_object(review, "blob", oid)
                checked.add(oid)

        # ls-tree -rだけでは途中ディレクトリの内容ハッシュを検証できないため、
        # -tで中間treeも列挙して確認する。パスをコマンドの引数へ流さない。
        records = review.run("ls-tree", "-r", "-t", "-z", "--full-tree", commit).split(b"\0")
        if len(records) - 1 > 2 * MAX_FILES:
            raise ReviewLimit("Review snapshot has too many entries")
        for record in records:
            if not record:
                continue
            metadata, path = record.split(b"\t", 1)
            valid_path(path.decode("utf-8"))
            _, kind, oid = metadata.decode("ascii").split()
            if kind == "tree" and oid not in checked:
                verified_object(review, "tree", oid)
                checked.add(oid)


def decide_change_request(db, storage, team_id, project_id, request_id, user_id, decision, reason=None):
    """OPEN/PENDINGの依頼を一回だけ判定し、確定した依頼を返す。

    ロック順はプロジェクトのファイルロック→Project/所属→依頼行→Git参照。
    判定行の主キーを依頼IDにするDB制約も、最後の二重登録防止として働く。
    同じ内容の再送でも再判定は409とし、応答不明時は詳細APIで結果を確認する。
    """
    try:
        with ReviewGit(storage).session(project_id) as review:
            # 長い管理トランザクションがある場合も、APIが無期限に行ロックを待たない。
            # SET LOCALなので、この接続をプールへ戻した後の別リクエストには設定を残さない。
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET LOCAL lock_timeout = '5s'"))
            # API入口でも確認するが、トランザクション内で現在の所属と対応を再確認する。
            # Project移動やメンバー削除が同時に行われても、コミットまでは対応を維持する。
            project = db.query(models.Project).filter_by(id=project_id, team_id=team_id).with_for_update(read=True).first()
            if project is None:
                raise ReviewNotFound()
            member = db.query(models.TeamMember).filter_by(team_id=team_id, user_id=user_id).with_for_update(
                read=True, key_share=True,
            ).first()
            if member is None:
                raise ReviewForbidden()

            # 認証処理などで同じ依頼を先に読み込んでいても、その古い値を使わない。
            # PostgreSQLの行ロック取得後にpopulate_existingでDBの現在値へ読み直す。
            # SQLiteには同等の行ロックがないため、そちらは同一ホストのflockと主キー制約が防御となる。
            row = db.query(models.ChangeRequest).filter_by(id=request_id, project_id=project_id).populate_existing().with_for_update(
                of=models.ChangeRequest,
            ).first()
            if row is None:
                raise ReviewNotFound()
            if row.submitted_by == user_id:
                raise ReviewForbidden()
            existing = db.query(models.ChangeRequestReview).filter_by(change_request_id=request_id).first()
            if row.state != "OPEN" or existing is not None:
                raise StorageConflict("Only open pending requests can be reviewed")
            if decision not in ("APPROVED", "CHANGES_REQUESTED"):
                raise StorageConflict("Unsupported review decision")

            # ロック保持中に提出時の記録と参照を照合し、DBコミットまで参照を固定する。
            # 判定APIからensure_refsは呼ばないため、欠落や不一致を上書きで直さない。
            manifest = row_manifest(row)
            with review.locked_review_refs(manifest["ref_key"]):
                verify_snapshot(review, row)
                result = models.ChangeRequestReview(
                    change_request_id=row.id, decision=decision, reviewer_id=user_id,
                    reviewed_at=datetime.utcnow(), reason=reason,
                )
                db.add(result)
                db.commit()

                # Responseのために判定を再取得する。提出のstate・コミット・日時は変更しない。
                db.refresh(row)
                return row
    except Exception:
        # Git検証、INSERT、コミットのどこで失敗しても未確定のDB変更は破棄する。
        # Git参照、Gitオブジェクト、作業ファイルを削除する補償処理は行わない。
        db.rollback()
        raise
