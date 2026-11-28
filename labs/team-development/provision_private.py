"""承認後、新規のprivate領域だけへ鍵・台帳・合成TLSを作成する。

コンテナ内の対象UIDで実行する。既存ファイルの上書き・chown・chmod修復はしない。
このプログラムは接続やサービス起動を行わず、秘密値を表示しない。
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys


def provision(directory, kind):
    if sys.platform!='linux' or os.geteuid()==0:raise ValueError('Non-root Linux required')
    # 配置imageの固定WORKDIR。利用者workspaceをimport探索先に追加しない。
    sys.path.insert(0,'/app')
    from app.submission_ledger import private_parent
    root=Path(directory)
    if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in [root,*root.parents]):raise ValueError('Unsafe private root')
    private_parent(root/'guard')
    if any(root.iterdir()):raise ValueError('New empty private directory required')
    def write(name,data):
        fd=os.open(root/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    if kind=='keys':
        for name in ('request.key','response.key','authorization.key'):write(name,secrets.token_bytes(64))
    elif kind=='database-password':
        write('database-password',secrets.token_urlsafe(32).encode('ascii')+b'\n')
    elif kind=='workspace-ledger':
        from app.workspace_journal import WorkspaceJournal
        WorkspaceJournal.provision(root/'initialization.sqlite')
    elif kind=='replay-ledger':
        from app.submission_ledger import SQLiteReplayLedger
        SQLiteReplayLedger.provision(root/'requests.sqlite')
    elif kind=='tls':
        from datetime import datetime,timedelta,timezone
        from ipaddress import ip_address
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes,serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'stage10-synthetic-only')])
        now=datetime.now(timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
              .not_valid_after(now+timedelta(days=7))
              .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address('127.0.0.1')),x509.DNSName('localhost')]),critical=False)
              .sign(key,hashes.SHA256()))
        write('server.key',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        write('server.crt',cert.public_bytes(serialization.Encoding.PEM))
    else:raise ValueError('Unknown provision purpose')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--approved-synthetic',action='store_true')
    parser.add_argument('--directory',required=True)
    parser.add_argument('--kind',required=True,choices=['keys','workspace-ledger','replay-ledger','tls','database-password'])
    args=parser.parse_args()
    if not args.approved_synthetic:raise SystemExit('別途承認が必要です')
    try:provision(args.directory,args.kind);print(json.dumps({'status':'PROVISIONED','purpose':args.kind}))
    except Exception:raise SystemExit('専用領域の初期化を拒否しました。既存ファイルは再利用しません。') from None
