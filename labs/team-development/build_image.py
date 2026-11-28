"""buildの終了0と新規iidfile・image inspectの一致を必須とする。存在だけで成功にしない。"""
import argparse
import json
from pathlib import Path
import re
import subprocess


def build(base,output):
    if not re.fullmatch(r'python:3\.11(?:\.\d+)?-slim(?:-[a-z]+)?@sha256:[0-9a-f]{64}',base):raise ValueError('Pinned Python base required')
    output=Path(output).absolute()
    if output.exists() or any(p.is_symlink() for p in [output,*output.parents]):raise ValueError('New build receipt required')
    iid=output.with_suffix('.iid')
    if iid.exists():raise ValueError('Existing build identity')
    root=Path(__file__).resolve().parents[2]
    # stdout/stderrの生ログは出さない。失敗時も既存image・中間imageを削除しない。
    result=subprocess.run(['docker','build','--iidfile',str(iid),'--build-arg','STAGE10_PYTHON_BASE='+base,
        '-f',str(root/'backend/Dockerfile.stage10d'),str(root/'backend')],capture_output=True,timeout=1800)
    if result.returncode!=0:raise ValueError('Build did not complete successfully')
    identity=iid.read_text().strip()
    if not re.fullmatch('sha256:[0-9a-f]{64}',identity):raise ValueError('Invalid image identity')
    inspected=subprocess.run(['docker','image','inspect','--format','{{.Id}}',identity],capture_output=True,text=True,check=True,timeout=15)
    if inspected.stdout.strip()!=identity:raise ValueError('Image identity mismatch')
    with output.open('x',encoding='utf-8') as stream:json.dump({'state':'BUILD_EXIT_0_INSPECT_MATCH','image':identity,'base':base},stream)
    return identity


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--base',required=True);parser.add_argument('--receipt',required=True)
    args=parser.parse_args()
    try:print(json.dumps({'status':'BUILT','image':build(args.base,args.receipt)}))
    except Exception:raise SystemExit('build完了を確認できません。既存imageを成功の代わりに使用しないでください。') from None
