"""DB登録とGit操作の順序を管理する。既存データを取り消しで削除しない。"""

from datetime import datetime
import uuid

from app import models
from app.project_storage import StorageConflict
from app.review_git import ReviewGit, ReviewInvalid, ReviewLimit, MAX_TOTAL_BYTES, MAX_FILE_BYTES


def reference_key(project_id, user_id, request_key):
    # UUID形式の提出キーだけから固定名を作る。ファイル名やUser IDを自由入力させない。
    return uuid.uuid5(uuid.NAMESPACE_URL, f"team-progress/reviews/{project_id}/{user_id}/{request_key}").hex


def row_manifest(row):
    key = reference_key(row.project_id, row.submitted_by, row.request_key)
    if row.git_ref != f"refs/change-requests/{key}/head":
        raise StorageConflict("Review database reference does not match")
    return {"version": 1, "ref_key": key, "project_id": row.project_id, "submitted_by": row.submitted_by,
            "request_key": row.request_key, "title": row.title, "description": row.description,
            "created_at": row.created_at.isoformat(), "base_commit": row.base_commit,
            "snapshot_commit": row.snapshot_commit}


def submit_change_request(db, storage, project_id, user_id, payload):
    request_key = str(payload.request_key)
    key = reference_key(project_id, user_id, request_key)
    with ReviewGit(storage).session(project_id) as review:
        # ロック取得後にDBを再確認する。同じキーの同時提出は一件にまとまる。
        row = db.query(models.ChangeRequest).filter_by(
            project_id=project_id, submitted_by=user_id, request_key=request_key,
        ).first()
        common = {"version": 1, "ref_key": key, "project_id": project_id, "submitted_by": user_id,
                  "request_key": request_key, "title": payload.title, "description": payload.description}
        manifest = review.manifest(key)
        if row:
            expected = row_manifest(row)
            if manifest is not None and manifest != expected:
                raise StorageConflict("Review database and journal do not match")
            manifest = expected
        if manifest is not None:
            if (not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in common.items())
                    or set(manifest) != set(common) | {"created_at", "base_commit", "snapshot_commit"}):
                raise StorageConflict("Request key already used with different data or invalid journal")
            # Gitオブジェクトが残っている場合だけ、欠けた参照を復旧する。既存の参照は上書きしない。
            review.ensure_refs(manifest)
            review.save_manifest(manifest)
        else:
            files = review.capture(user_id, project_id)
            base = review.main_commit()
            baseline = review.entries(base)
            sizes = [int(review.run("cat-file", "-s", oid)) for _, oid in baseline.values()]
            if sum(sizes) > MAX_TOTAL_BYTES or any(size > MAX_FILE_BYTES for size in sizes):
                raise ReviewLimit("Shared baseline exceeds the review limit")
            tree = review.tree(files)
            if tree == review.run("rev-parse", base + "^{tree}").decode().strip():
                raise StorageConflict("No changes to submit")
            head = review.run("commit-tree", tree, "-p", base, data=("Review snapshot " + key + "\n").encode()).decode().strip()
            manifest = {**common, "created_at": datetime.utcnow().isoformat(), "base_commit": base, "snapshot_commit": head}
            # 復旧記録→参照→DBの順。DB失敗時も同じキーで同じスナップショットを再登録できる。
            review.save_manifest(manifest)
            review.ensure_refs(manifest)
        if row:
            return row, False
        try:
            created_at = datetime.fromisoformat(manifest["created_at"])
        except (ValueError, TypeError) as error:
            raise StorageConflict("Review journal timestamp is invalid") from error
        row = _row_from_manifest(manifest)
        try:
            db.add(row)
            db.commit()
            db.refresh(row)
        except Exception:
            # Gitの参照や元ファイルは消さない。記録を残して安全に再試行する。
            db.rollback()
            raise
        return row, True


def _row_from_manifest(manifest):
    """既存経路・タスク変換経路でDB列の対応を共有する。commitは呼出し元が行う。"""
    created_at=datetime.fromisoformat(manifest['created_at'])
    return models.ChangeRequest(project_id=manifest['project_id'],submitted_by=manifest['submitted_by'],
        request_key=manifest['request_key'],title=manifest['title'],description=manifest['description'],state='OPEN',
        base_commit=manifest['base_commit'],snapshot_commit=manifest['snapshot_commit'],
        git_ref='refs/change-requests/'+manifest['ref_key']+'/head',created_at=created_at,updated_at=created_at)


def register_review_snapshot(db,review,manifest,*,repair=True):
    """認可済み内部サービス専用。公開APIへ任意commit受付を追加しない。

    Projectロック下で既知のmanifestを照合し、既存snapshot検証を通してからflushする。
    db.commitを内部で行わず、変換対応表と同一トランザクションで確定できるようにする。
    """
    from app.change_request_reviews import verify_snapshot
    key=reference_key(manifest['project_id'],manifest['submitted_by'],manifest['request_key'])
    if manifest['ref_key']!=key:raise StorageConflict('Invalid review snapshot identity')
    existing=review.manifest(key)
    row=db.query(models.ChangeRequest).filter_by(project_id=manifest['project_id'],
        submitted_by=manifest['submitted_by'],request_key=manifest['request_key']).first()
    if existing is not None and existing!=manifest:raise StorageConflict('Review journal differs')
    if row is not None and row_manifest(row)!=manifest:raise StorageConflict('Review database differs')
    if not repair and (row is None or existing is None):raise StorageConflict('Review evidence missing')
    if existing is None:
        if row is not None or any(review.read_ref(('change-requests',key,part)) is not None for part in ('base','head')):
            raise StorageConflict('Review journal missing for existing evidence')
        review.save_manifest(manifest)
    if repair:review.ensure_refs(manifest)
    candidate=row if row is not None else _row_from_manifest(manifest)
    verify_snapshot(review,candidate)
    if row is None:db.add(candidate);db.flush()
    return candidate


def change_request_diff(storage, row):
    with ReviewGit(storage).session(row.project_id) as review:
        try:
            return review.diff(row_manifest(row))
        except UnicodeError as error:
            raise ReviewInvalid("Review file names or references are not valid UTF-8") from error
