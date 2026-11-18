"""検証イメージの誤起動を拒否する。引数・環境変数でcode-serverへ切り替えない。"""

import sys


def main():
    """未検証のイメージから実エディタを起動せず、秘密を含まない固定理由で終了する。"""
    sys.stderr.write("gvisor_lab_runtime_disabled\n")
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
