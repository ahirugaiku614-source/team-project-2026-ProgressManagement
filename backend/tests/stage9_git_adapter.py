"""合成デモ専用のWindows Git OSアダプター。Linux FD・fsync保証の代替ではない。"""
from contextlib import contextmanager
from pathlib import Path
import json
import os

from app.integration_git import operation_manifest
from app.project_storage import StorageConflict


class PortableJournal:
    def __init__(self, review, directory):self.directory=Path(directory)
    def read(self,name='active.json'):
        path=self.directory/name
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    def prepare(self,row):
        with (self.directory/'active.json').open('x',encoding='utf-8') as stream:
            json.dump(operation_manifest(row),stream);stream.flush();os.fsync(stream.fileno())
    def sync_active(self,row):
        if self.read()!=operation_manifest(row):raise StorageConflict('Synthetic journal mismatch')
    def verify_archive(self,row):
        if self.read(row.operation_id+'.json')!=operation_manifest(row):raise StorageConflict('Synthetic archive mismatch')
    def archive(self,row):
        self.sync_active(row)
        with (self.directory/(row.operation_id+'.json')).open('x',encoding='utf-8') as stream:
            json.dump(operation_manifest(row),stream);stream.flush();os.fsync(stream.fileno())
        (self.directory/'active.json').unlink()


@contextmanager
def directory(parent,name):
    path=Path(parent)/name;path.mkdir(exist_ok=True)
    yield path


@contextmanager
def locked_main(review):
    # 実Gitのupdate-refで旧OIDのCASを行う。Linuxのmain.lock/fsyncは未実証。
    with review.lock:
        def publish(old,new):review.run('update-ref','refs/heads/main',new,old or '0'*40)
        yield publish


def sync_main(review,expected):
    if review.read_ref(('heads','main'))!=expected:raise StorageConflict('Synthetic main mismatch')
