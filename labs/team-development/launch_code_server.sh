#!/bin/sh
# 管理された新規環境専用。既存コンテナのentrypointやvolumeを変更しない。
# image内の固定バイナリを事前確認し、保存時の新規ファイルをprivateにする。
set -eu
umask 077
test -x /usr/bin/code-server || { echo '固定code-server入口を確認できません' >&2; exit 2; }
exec /usr/bin/code-server "$@"
