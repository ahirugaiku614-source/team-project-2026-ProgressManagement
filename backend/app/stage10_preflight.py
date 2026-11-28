"""Stage 10-Bの合成環境で使う読取り専用メタデータ検査。本文・鍵は読まない。

この検査の成功はdirty検証や提出許可ではない。Linuxへの配置承認後、管理者が
登録済みのsourceと検証用ファイルを指定する。chmod・mount・socket作成はしない。
"""
import json
import os
from pathlib import Path
import stat
import sys


def regular_file_safe(info, expected_uid):
    """製品収集器と同じ所有者・通常ファイル・単一リンク・書込み権限条件。"""
    return (type(expected_uid) is int and expected_uid > 0
            and stat.S_ISREG(info.st_mode) and info.st_uid == expected_uid
            and info.st_nlink == 1 and not info.st_mode & 0o022)


def inspect_source(source, relative_file, expected_uid):
    """FD相対で明示した一つの合成ファイルの属性だけを見る。本文は開かない。"""
    from app.submission_scope import normalize_folder_path
    normalize_folder_path(relative_file)
    path = Path(source)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Invalid fixed source')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            if info.st_uid not in (0, expected_uid) or info.st_mode & 0o022:
                raise ValueError('Unsafe source ancestor')
        root = os.fstat(fd)
        if root.st_uid != expected_uid:
            raise ValueError('Unexpected source owner')
        readonly = bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY)
        parts = relative_file.split('/')
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            if info.st_uid != expected_uid or info.st_mode & 0o022 or info.st_dev != root.st_dev:
                raise ValueError('Unsafe task ancestor')
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        return {'readonly_mount': readonly,
                'file_metadata_safe': regular_file_safe(info, expected_uid) and info.st_dev == root.st_dev}
    finally:
        os.close(fd)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='承認後の合成領域メタデータ検査。本文は取得しません。')
    parser.add_argument('--approved-synthetic', action='store_true')
    parser.add_argument('--source', required=True)
    parser.add_argument('--relative-file', required=True)
    parser.add_argument('--expected-uid', required=True, type=int)
    args = parser.parse_args()
    if not args.approved_synthetic or sys.platform != 'linux' or os.geteuid() == 0:
        print(json.dumps({'status': 'REFUSED', 'reason': 'APPROVED_NONROOT_LINUX_REQUIRED'}))
        return 2
    try:
        result = inspect_source(args.source, args.relative_file, args.expected_uid)
        result['status'] = 'PASS' if all(result.values()) else 'REFUSED'
        result['submission_enabled'] = False
        print(json.dumps(result))
        return 0 if result['status'] == 'PASS' else 2
    except Exception:
        # 内部パス・例外文字列・ファイル名を標準出力へ含めない。
        print(json.dumps({'status': 'REFUSED', 'reason': 'SOURCE_METADATA_UNAVAILABLE'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
