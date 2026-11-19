"""専用workerの既定入口。接続が未承認なのでサーバーもEngineも起動しない。"""

import json


def main():
    """配備を許可するflagは持たず、固定の未接続状態だけを表示する。"""
    print(json.dumps({"worker_connection": "DISABLED", "docker": "NOT_RUN", "launch_enabled": False}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
