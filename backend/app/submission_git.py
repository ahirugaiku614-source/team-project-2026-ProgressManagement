"""Dedicated immutable submission journal and refs, using server-owned Git.

    Not wired to a real root/provider by default. Linux operations remain Linux-only.
"""
from contextlib import contextmanager
from dataclasses import asdict
import json
from uuid import UUID
from app.review_git import ReviewGit
from app.project_storage import StorageConflict
from app.submission_provider import SubmissionSnapshot, ProviderUnavailable, EvidenceMismatch


class StoredEvidenceMismatch(StorageConflict, EvidenceMismatch):
    """既存StorageConflict契約を保ち、確定した証拠不整合を型で識別する。"""


def identity(request):
    for value in (request.submission_id, request.request_id, request.editing_session_id):
        if str(UUID(value)) != value:
            raise StorageConflict('Noncanonical submission identifier')
    return {'version': 1, 'submission_id': request.submission_id,
            'request_id': request.request_id, 'editing_session_id': request.editing_session_id,
            'target': asdict(request.target)}


def decode_record(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise StoredEvidenceMismatch('Duplicate submission journal key')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)
    except (ValueError, UnicodeError):
        raise StoredEvidenceMismatch('Invalid submission journal') from None


def exact(left, right):
    # JSON number/bool equality must not make corrupt identity evidence valid.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


class SubmissionReviewGit(ReviewGit):
    def journal_read(self, submission_id, phase):
        try:
            with self.storage._directory(self.project, 'submission-journal') as journal:
                return decode_record(self.read(journal, submission_id + '.' + phase))
        except FileNotFoundError:
            return None

    def journal_create(self, submission_id, phase, record):
        with self.new_directory(self.project, 'submission-journal') as journal:
            name = submission_id + '.' + phase
            # Exclusive creation; no retry converts an existing intent to a new run.
            try:
                self.read(journal, name)
            except FileNotFoundError:
                self.write_once(journal, name, json.dumps(record, sort_keys=True).encode('utf-8'))
            else:
                raise StorageConflict('Submission journal already exists')

    def submission_ref(self, submission_id):
        loose = self.read_ref(('task-submissions', submission_id))
        packed = self.packed_refs().get(('refs/task-submissions/' + submission_id).encode())
        if packed is not None and loose != packed:
            raise StoredEvidenceMismatch('Submission packed reference mismatch')
        return loose

    def create_submission_ref(self, submission_id, commit):
        self.commit_exists(commit)
        with self.git_lock(self.shared, 'packed-refs.lock'), self.new_directory(self.shared, 'refs') as refs:
            with self.new_directory(refs, 'task-submissions') as directory:
                with self.git_lock(directory, submission_id + '.lock'):
                    if self.submission_ref(submission_id) is not None:
                        raise StorageConflict('Submission reference already exists')
                    self.write_once(directory, submission_id, (commit + '\n').encode('ascii'))


class GitSnapshotStore:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    @classmethod
    def for_managed_storage(cls, storage):
        return cls(lambda project_id: SubmissionReviewGit(storage).session(project_id))

    def save(self, request, collect, authorize):
        expected = identity(request)
        with self.session_factory(request.target.project_id) as git:
            # Durable intent BEFORE reading files. Any existing intent forbids recapture.
            if git.journal_read(request.submission_id, 'intent') is not None:
                raise ProviderUnavailable('Existing submission requires explicit reconciliation')
            authorize()
            git.journal_create(request.submission_id, 'intent', expected)
            files = collect()
            authorize()
            tree = git.tree(files)  # inherits safe hash-object/mktree plumbing
            # Parentless tree contains only this task, never a full shared baseline.
            commit = git.run('commit-tree', tree, data=b'Task submission\n').decode('ascii').strip()
            receipt = SubmissionSnapshot(request.submission_id, request.target,
                request.editing_session_id, 'refs/task-submissions/' + request.submission_id,
                commit, len(files), sum(len(body) for _, body in files.values()), True)
            prepared = {**expected, 'receipt': asdict(receipt)}
            git.journal_create(request.submission_id, 'prepared', prepared)
            authorize()
            git.create_submission_ref(request.submission_id, commit)
            authorize()
            git.journal_create(request.submission_id, 'complete', prepared)
            self._verify(git, request)
            return receipt

    def _verify(self, git, request):
        expected = identity(request)
        intent = git.journal_read(request.submission_id, 'intent')
        prepared = git.journal_read(request.submission_id, 'prepared')
        complete = git.journal_read(request.submission_id, 'complete')
        if (not exact(intent, expected) or not isinstance(prepared, dict) or not exact(prepared, complete)
                or set(prepared) != set(expected) | {'receipt'}
                or any(not exact(prepared[k], value) for k, value in expected.items())):
            raise StoredEvidenceMismatch('Incomplete or inconsistent submission journal')
        record = prepared['receipt']
        required = {'submission_id', 'target', 'editing_session_id', 'git_ref', 'commit_id',
                    'file_count', 'total_bytes', 'saved_files_confirmed'}
        if (not isinstance(record, dict) or set(record) != required
                or record['submission_id'] != request.submission_id
                or not exact(record['target'], asdict(request.target))
                or record['editing_session_id'] != request.editing_session_id
                or record['git_ref'] != 'refs/task-submissions/' + request.submission_id
                or record['saved_files_confirmed'] is not True
                or type(record['file_count']) is not int or not 1 <= record['file_count'] <= 1000
                or type(record['total_bytes']) is not int or not 0 <= record['total_bytes'] <= 10485760
                or git.submission_ref(request.submission_id) != record['commit_id']):
            raise StoredEvidenceMismatch('Submission reference and journal disagree')
        git.commit_exists(record['commit_id'])
        commit = record['commit_id']
        parents = git.run('rev-list', '--parents', '-n', '1', commit).split()
        if parents != [commit.encode('ascii')]:
            raise StoredEvidenceMismatch('Submission commit must be task-only and parentless')
        commit_bytes = git.run('cat-file', 'commit', commit)
        if git.run('hash-object', '-t', 'commit', '--stdin', data=commit_bytes).decode().strip() != commit:
            raise StoredEvidenceMismatch('Submission commit object is corrupt')
        raw_entries = git.run('ls-tree', '-r', '-z', '--full-tree', commit).split(b'\0')[:-1]
        raw_paths = [entry.split(b'\t', 1)[1] for entry in raw_entries]
        if len(raw_paths) != len(set(raw_paths)):
            raise StoredEvidenceMismatch('Submission tree has duplicate entries')
        entries = git.entries(record['commit_id'])
        if len(entries) != record['file_count']:
            raise StoredEvidenceMismatch('Submission tree count mismatch')
        total = 0
        from app.submission_collection import excluded
        from app.submission_targets import normalize_folder_path
        for path, (mode, oid) in entries.items():
            normalize_folder_path(path)
            if mode != '100644' or any(excluded(part) for part in path.split('/')):
                raise StoredEvidenceMismatch('Unsafe stored submission tree')
            size = int(git.run('cat-file', '-s', oid))
            if size > 1048576:
                raise StoredEvidenceMismatch('Stored submission file exceeds limit')
            body = git.blob(oid)
            if len(body) != size or git.run('hash-object', '--stdin', data=body).decode().strip() != oid:
                raise StoredEvidenceMismatch('Submission blob object is corrupt')
            total += size
        if total != record['total_bytes']:
            raise StoredEvidenceMismatch('Submission tree size mismatch')
        return SubmissionSnapshot(**{**record, 'target': request.target})

    def recover(self, request):
        # Read-only reconciliation: never recollect, recreate refs or rewrite journals.
        identity(request)
        with self.session_factory(request.target.project_id) as git:
            return self._verify(git, request)


class ManagedSnapshotProvider:
    """Explicitly injected tests only. No environment switch or default registration.

    source(request) supplies a server-bound root+ops. authorize(request) rechecks
    current DB authorization/state; saved_session(request) must attest saved state.
    No client path, arbitrary URL or editor buffer content enters this interface.
    """
    def __init__(self, source, store, authorize, saved_session, *, collector=None,
                 verify_authorize=None, recover_authorize=None):
        self.source, self.store = source, store
        self.authorize, self.saved_session = authorize, saved_session
        # collector指定時だけ内部RPC経由の収集へ差し替える。Git保存先は変えない。
        # 既存のローカル偽FDテストは従来どおりsourceを使用できる。
        self.collector = collector
        # 用途ごとに接続し、収集開始用PROCESSING認可を過去の証拠へ流用しない。
        # 既存のローカル偽providerは従来の認可を維持し、RPC factoryだけが
        # 現在の権限と保存済み対応を検証する専用コールバックを明示注入する。
        self.verify_authorize = authorize if verify_authorize is None else verify_authorize
        self.recover_authorize = authorize if recover_authorize is None else recover_authorize

    def ensure_ready(self):
        pass

    def collect(self, request):
        from app.submission_collection import collect_task
        def check():
            if self.authorize(request) is not True or self.saved_session(request) is not True:
                raise ProviderUnavailable('Authorization or saved session unavailable')
        def collect():
            if self.collector is not None:
                return self.collector(request)
            with self.source(request) as (root, ops):
                return collect_task(request, root, ops)
        return self.store.save(request, collect, check)

    def verify(self, request, receipt):
        if self.verify_authorize(request) is not True:
            raise ProviderUnavailable('Snapshot authorization unavailable')
        if self.store.recover(request) != receipt:
            raise EvidenceMismatch('Snapshot verification failed')

    def verify_collection(self, request, receipt):
        """新規収集をDBで公開する直前の検査。履歴の読取りとは用途が異なる。

        Git保存後でもPROCESSING・許可期限・現在の保存状態を要求する。
        証拠を読む間に期限や保存状態が変わった場合も公開成功にしない。
        失敗しても既存Git証拠や消費済み受付は戻さず、明示復旧へ引き継ぐ。
        """
        def current():
            if self.authorize(request) is not True or self.saved_session(request) is not True:
                raise ProviderUnavailable('Collection publication authorization unavailable')
        current()
        self.verify(request, receipt)
        current()

    def recover(self, request):
        if self.recover_authorize(request) is not True:
            raise ProviderUnavailable('Recovery authorization unavailable')
        return self.store.recover(request)
