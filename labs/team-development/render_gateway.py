"""固定2環境だけのgateway設定生成。既存ファイルを上書きせず、起動もしない。"""
from pathlib import Path
from uuid import UUID
import argparse
import os


def render(template, member_a, member_b):
    for value in (member_a, member_b):
        if str(UUID(value)) != value:raise ValueError('Invalid workspace identifier')
    if member_a == member_b:raise ValueError('Workspaces must be separate')
    # nginx自身の$host等は置換しない。任意のupstreamやパスは受け付けない。
    return template.replace('${WORKSPACE_A_ID}', member_a).replace('${WORKSPACE_B_ID}', member_b)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='新規gateway設定の生成だけを行います')
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    value=render(Path(__file__).with_name('nginx.conf.template').read_text(encoding='utf-8'),
                 os.environ['WORKSPACE_A_ID'],os.environ['WORKSPACE_B_ID'])
    with args.output.open('x',encoding='utf-8') as stream:stream.write(value)
