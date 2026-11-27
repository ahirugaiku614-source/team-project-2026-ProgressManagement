"""固定workspace UUIDだけを置換する。nginxの$変数をenvsubstで消さない。"""
import argparse
import os
from pathlib import Path
from uuid import UUID


def render(template,a,b):
    """既存の純粋な文字列変換APIを維持する。任意upstreamは受け取らない。"""
    if any(str(UUID(value))!=value for value in (a,b)) or a==b:raise ValueError('Distinct workspace UUIDs required')
    return template.replace('${WORKSPACE_A_ID}',a).replace('${WORKSPACE_B_ID}',b)


def render_file(a,b,output):
    output=Path(output)
    if output.exists() or any(p.is_symlink() for p in [output,*output.parents]):raise ValueError('New configuration required')
    template=Path(__file__).with_name('nginx.conf.template').read_text(encoding='utf-8')
    if any(marker not in template for marker in ('${WORKSPACE_A_ID}','${WORKSPACE_B_ID}')):raise ValueError('Template mismatch')
    template=render(template,a,b)
    fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:stream.write(template)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--a');parser.add_argument('--b');parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:render_file(args.a or os.environ['WORKSPACE_A_ID'],args.b or os.environ['WORKSPACE_B_ID'],args.output)
    except Exception:raise SystemExit('固定gateway設定の作成を拒否しました') from None
