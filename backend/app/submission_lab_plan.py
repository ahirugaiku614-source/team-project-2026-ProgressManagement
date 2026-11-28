"""Compose案の承認前メタデータ検査。環境変数・秘密・実FSへはアクセスしない。"""
from pathlib import PurePosixPath
import re


def validate_lab_plan(values, approved_root):
    """管理者が渡した非秘密の設定表だけを検証する。

    Compose自身ではUID重複やbind元包含を禁止できないため、別の承認条件とする。
    この関数の成功は既存パス・symlink・実UIDマッピングの確認を意味しない。
    """
    def fail(): raise ValueError('隔離環境の構成条件を満たしていません')
    root = PurePosixPath(approved_root)
    if not root.is_absolute() or str(root) in ('/', '/home', '/srv') or '..' in root.parts: fail()
    identities = []
    for name in ('STUDENT_A', 'STUDENT_B', 'COLLECTOR_A', 'COLLECTOR_B', 'MANAGER', 'POSTGRES'):
        pair = tuple(values.get(name + '_' + role) for role in ('UID', 'GID'))
        if any(type(v) is not int or not 1 <= v <= 2**31 - 1 for v in pair): fail()
        identities.append(pair)
    # 収集者にも別の実行IDを要求する。読取りは別途承認するACL等で限定する。
    if len({p[0] for p in identities}) != 6 or len({p[1] for p in identities}) != 6: fail()
    paths = []
    for key in ('STUDENT_A_PROJECT_ROOT', 'STUDENT_B_PROJECT_ROOT', 'COLLECTOR_A_SOCKET_DIR',
                'COLLECTOR_B_SOCKET_DIR', 'COLLECTOR_A_LEDGER_DIR', 'COLLECTOR_B_LEDGER_DIR',
                'COLLECTOR_A_KEYS_DIR', 'COLLECTOR_B_KEYS_DIR', 'MANAGED_GIT_ROOT', 'SUBMISSION_DB_ROOT',
                'STUDENT_A_CONFIG', 'STUDENT_B_CONFIG', 'MANAGER_FIREBASE_CREDENTIAL_FILE'):
        value = values.get(key)
        if type(value) is not str: fail()
        path = PurePosixPath(value)
        if (not path.is_absolute() or str(path) != value or '..' in path.parts
                or root not in path.parents or any(p.startswith('.') for p in path.parts[1:])): fail()
        paths.append(path)
    # 学生領域に管理領域が含まれる・相互に共有される構成を拒否する。
    for i, left in enumerate(paths):
        for right in paths[i + 1:]:
            if left == right or left in right.parents or right in left.parents: fail()
    ports = [values.get('STUDENT_A_PORT'), values.get('STUDENT_B_PORT')]
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or ports[0] == ports[1]: fail()
    for key in ('SUBMISSION_CODE_IMAGE', 'SUBMISSION_MANAGER_IMAGE', 'SUBMISSION_POSTGRES_IMAGE'):
        value = values.get(key)
        if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}', value): fail()
    return True
