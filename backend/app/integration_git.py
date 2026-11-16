"""統合だけに必要な、main参照のCASと永続的な操作記録。

ユーザーの.gitは開かない。ReviewGit.sessionとlocked_review_refsの内側から呼び、
Projectのflock・packed-refs.lockを保持した状態でGit標準のmain.lockを取得する。
既存の共有Git設定やフックを読み込まず、mainのloose参照だけを原子的に置換する。
"""

from contextlib import contextmanager
import json
import os
import stat
import uuid

from app.change_request_reviews import verified_object
from app.project_storage import StorageConflict, StorageUnavailable

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def canonical_operation_id(value):
    """DB・保存済みJSONの値も正規UUIDに限定し、パス文字列として信用しない。"""
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as error:
        raise StorageConflict("Integration operation identifier is invalid") from error
    return value


def verify_initial_base(review, base):
    """main_commit()を呼び直さず、提出時の空ルートcommitそのものを検査する。

    従来の空baseは生成時刻を含むため、作り直したIDとの比較はできない。
    内容ハッシュ、空tree、親がないこと、生成メッセージを確認する。
    このbaseを親に持つ提出commitをmainとすれば、初回も履歴を切断しない。
    """
    raw = verified_object(review, "commit", base)
    headers, message = raw.split(b"\n\n", 1)
    trees = [line[5:] for line in headers.splitlines() if line.startswith(b"tree ")]
    if (trees != [EMPTY_TREE.encode()] or any(line.startswith(b"parent ") for line in headers.splitlines())
            or message != b"Empty review base\n" or verified_object(review, "tree", EMPTY_TREE) != b""):
        raise StorageConflict("Initial baseline is not a virtual empty root")


def operation_manifest(row):
    """DBの不変な準備情報だけをJSON化する。確定日時は後からDBへ保存する。"""
    return {"version": 1, "operation_id": canonical_operation_id(row.operation_id), "project_id": row.project_id,
            "change_request_id": row.change_request_id, "integrated_by": row.integrated_by,
            "prepared_at": row.prepared_at.isoformat(), "main_before": row.main_before,
            "main_after": row.main_after}


class IntegrationJournal:
    """activeを障害時の関門とし、完了後も削除せず操作ID名へ移して履歴を残す。"""
    def __init__(self, review, directory):
        self.review, self.directory = review, directory

    def read(self, name="active.json"):
        # DBが破損していても、../等をos.openへ渡さない。自由なファイル名は受け付けない。
        if name != "active.json":
            if not isinstance(name, str) or not name.endswith(".json"):
                raise StorageConflict("Integration journal name is invalid")
            canonical_operation_id(name[:-5])
        try:
            value = json.loads(self.review.read(self.directory, name))
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError) as error:
            raise StorageConflict("Integration journal is invalid; operator inspection required") from error
        # ファイル名に使うoperation_idも、保存済みファイルを信用せず検査する。
        if not isinstance(value, dict):
            raise StorageConflict("Integration journal has an invalid shape")
        try:
            canonical_operation_id(value["operation_id"])
        except KeyError as error:
            raise StorageConflict("Integration operation identifier is invalid") from error
        return value

    def prepare(self, row):
        self.review.write_once(self.directory, "active.json",
                               json.dumps(operation_manifest(row), sort_keys=True).encode())

    def archive(self, row):
        """DBの確定後にだけactiveを退避する。既存アーカイブを上書きしない。"""
        expected = operation_manifest(row)
        if self.read() != expected:
            raise StorageConflict("Integration journal and database disagree")
        # renameat2(RENAME_NOREPLACE)とfsyncで、別の記録を消さずに退避する。
        # 退避失敗時はCOMPLETEDとactiveを残す。再送が同じ処理を再実行できる。
        self.review.storage._publish(self.directory, "active.json", row.operation_id + ".json")


@contextmanager
def locked_main(review):
    """Git標準のmain.lockを取得し、現在値を検査するための取引を提供する。

    packed-refs.lockは呼び出し元で取得済み。looseがあればGitと同じくそれを優先し、
    packedに古い値が残る通常の状態も扱う。他者のロックは強制削除しない。
    """
    with review.storage._directory(review.shared, "refs") as refs:
        with review.new_directory(refs, "heads") as heads:
            try:
                fd = os.open("main.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=heads)
            except FileExistsError as error:
                raise StorageUnavailable("Main reference is locked") from error
            created = os.fstat(fd)
            consumed = False

            def check_owner():
                current = os.stat("main.lock", dir_fd=heads, follow_symlinks=False)
                if ((created.st_dev, created.st_ino) != (current.st_dev, current.st_ino)
                        or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                    raise StorageConflict("Main lock ownership changed; operator inspection required")

            def replace(expected, new):
                nonlocal consumed
                # 正式な参照に触れる前に内容をfsyncする。途中失敗はactiveとPREPAREDで追跡する。
                # 値はサーバー確定のOIDのみ。symrefや危険な既存ファイルはread_refが拒否する。
                review.oid(new)
                if expected is not None:
                    review.oid(expected)
                with os.fdopen(os.dup(fd), "wb") as output:
                    output.write((new + "\n").encode("ascii"))
                    output.flush()
                    os.fsync(output.fileno())
                check_owner()
                if review.read_ref(("heads", "main")) != expected:
                    raise StorageConflict("Main changed before publication")
                # 同じディレクトリ内のrenameがCASの公開点。
                # 標準ロックを守るGit操作はこの間mainを書き換えられない。
                os.replace("main.lock", "main", src_dir_fd=heads, dst_dir_fd=heads)
                consumed = True
                os.fsync(heads)

            try:
                yield replace
            finally:
                try:
                    if not consumed:
                        check_owner()
                        os.unlink("main.lock", dir_fd=heads)
                        os.fsync(heads)
                finally:
                    os.close(fd)
