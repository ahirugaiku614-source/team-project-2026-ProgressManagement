"""Fake FD tree + real disposable Git plumbing. Not a Windows FD adapter."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import json
import os
import stat
import subprocess
import threading
import time

from app.review_git import ReviewGit
from app.submission_git import decode_record


class FakeTaskOps:
    def __init__(self):
        self.nodes = {'': {}, 'assignments': {}, 'assignments/one': {},
                      'assignments/one/main.py': {'data': b'fixed fixture\n'},
                      'assignments/two': {}, 'assignments/two/private.py': {'data': b'sibling'}}
        self.handles = {3: ''}
        self.next = 10
        self.reads = []
        self.change_contents = False
        self.changed_identity = False

    def duplicate(self, fd):
        self.next += 1; self.handles[self.next] = self.handles[fd]; return self.next

    def open_directory(self, fd, name):
        path = '/'.join(filter(None, (self.handles[fd], name)))
        if path not in self.nodes: raise FileNotFoundError()
        self.next += 1; self.handles[self.next] = path; return self.next

    def metadata(self, path):
        node = self.nodes[path]
        mode = node.get('mode', (stat.S_IFREG | 0o600) if 'data' in node else (stat.S_IFDIR | 0o700))
        return SimpleNamespace(st_dev=node.get('device', 1), st_ino=node.get('inode', list(self.nodes).index(path) + 1),
            st_uid=node.get('uid', 1000), st_mode=mode, st_nlink=node.get('links', 1),
            st_size=node.get('size', len(node.get('data', b''))), st_mtime_ns=1, st_ctime_ns=1)

    def info(self, fd): return self.metadata(self.handles[fd])

    def names(self, fd):
        prefix = self.handles[fd]
        prefix = prefix + '/' if prefix else ''
        return sorted(path[len(prefix):] for path in self.nodes if path.startswith(prefix)
                      and path != prefix.rstrip('/') and '/' not in path[len(prefix):])

    def entry_info(self, fd, name):
        return self.metadata('/'.join(filter(None, (self.handles[fd], name))))

    def read_file(self, fd, name, expected):
        path = '/'.join(filter(None, (self.handles[fd], name)))
        self.reads.append(path)
        data = self.nodes[path]['data']
        if self.changed_identity: self.nodes[path]['inode'] = 999
        if self.change_contents and len(self.reads) > 1: data = bytes([data[0] ^ 1]) + data[1:]
        return data

    def close(self, fd): del self.handles[fd]


class PortableGitFixture(ReviewGit):
    """TEST ONLY: no student paths. Reuses product tree/entries/commit validation.

    Uses Git's update-ref create-only transaction for Windows tests. Linux product
    uses FD operations and standard Git locks; this fixture does not prove those.
    """
    def __init__(self, directory):
        self.repo = Path(directory) / 'server.git'
        self.journal = Path(directory) / 'journal'
        self.journal.mkdir()
        self.deadline = time.monotonic() + 300
        self.lock = threading.RLock()
        self.failure = None
        self.run('init', '--bare', '--object-format=sha1', '--template=', str(self.repo))

    def run(self, *args, data=b'', initial=False):
        if self.failure == 'git': raise RuntimeError('synthetic Git failure')
        env = {'PATH': os.environ['PATH'], 'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
            'HOME': str(self.repo.parent), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_ALLOW_PROTOCOL': '', 'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1',
            'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@localhost',
            'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@localhost'}
        if getattr(self,'review_metadata',False):
            # レビュー/初回統合の契約試験だけは既存ReviewGitと同じ固定主体・UTC形式にする。
            env.update(GIT_AUTHOR_NAME='Project Review',GIT_AUTHOR_EMAIL='review@localhost',
                       GIT_COMMITTER_NAME='Project Review',GIT_COMMITTER_EMAIL='review@localhost',
                       GIT_AUTHOR_DATE='@0 +0000',GIT_COMMITTER_DATE='@0 +0000')
        command = ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'protocol.allow=never',
                   '-c', 'gc.auto=0', '--git-dir=' + str(self.repo), *args]
        return subprocess.run(command, input=data, env=env, shell=False, capture_output=True,
                              timeout=10, check=True).stdout

    @contextmanager
    def session(self, project_id):
        with self.lock: yield self

    def journal_read(self, sid, phase):
        path = self.journal / (sid + '.' + phase)
        return decode_record(path.read_bytes()) if path.exists() else None

    def journal_create(self, sid, phase, record):
        if self.failure == phase: raise RuntimeError('synthetic journal failure')
        with (self.journal / (sid + '.' + phase)).open('xb') as stream:
            stream.write(json.dumps(record, sort_keys=True).encode())
            stream.flush(); os.fsync(stream.fileno())

    def submission_ref(self, sid):
        out = self.run('for-each-ref', '--format=%(objectname)', 'refs/task-submissions/' + sid).strip()
        return out.decode() if out else None

    def create_submission_ref(self, sid, commit):
        if self.failure == 'ref': raise RuntimeError('synthetic ref failure')
        self.run('update-ref', 'refs/task-submissions/' + sid, commit, '0' * 40)
