# Copilot引き継ぎ：Stage 10-Cの転送・配置・合成検証

実行承認後だけ使用する。CodexはLinuxへ接続・転送・起動していない。既存A/Bデモ、volume、PR #21、実DB、実Firebaseを変更しない。runcは合成専用。

## 1. 必要な成果物

Windowsの専用worktreeで次を実行する。既存ZIPがあれば上書きせず別途確認する。

```powershell
python labs/team-development/build_handoff.py --output "$PWD/frontend/artifacts/stage10c-handoff-02.zip"
```

archiveには現在の作業ツリーのbackend/app・migrations・tests、requirements/Dockerfile、frontend/src・testsとビルド設定、labs/team-development、labs/code-server-submit、team-development設計記録を明示範囲で含める。正確な全ファイルとSHA-256はstage10-manifest.jsonに記録する。Git履歴、認証情報、.env、node_modules、dist、生成HTML、SQLite、一時領域、バックアップは含めない。未コミットの今回のソースも含む。manifestは転送事故検出用であり署名ではない。

重要な入口：

- backend/app/stage10_asgi.py：通常認証を維持した明示配置用API。
- backend/app/workspace_bootstrap.py：初期化UDSサーバー。
- backend/app/snapshot_runtime.py：方式C収集UDSサーバー／管理側provider factory。
- backend/app/editor_socket_gateway.py：継続WebSocket失効監視。
- backend/app/managed_worker.py・worker_sandbox.py：独立子プロセス、Landlock。
- backend/migrations/005～011：明示移行。通常起動では適用しない。
- labs/team-development/compose.yaml + stage10.yaml + stage10c.yaml：新規管理構成。
- labs/team-development/settings-stage10.example.json + settings-snapshot.example.json：固定設定のスキーマ例。
- labs/team-development/launch_code_server.sh：新規code-server用umask077 launcher。
- labs/team-development/provision_private.py：新規専用鍵・台帳・TLSの初期化。
- labs/team-development/prepare_layout.py：共有されたrootless mapを照合した新規領域の所有者・権限準備。
- labs/team-development/stage10b-volume-audit.yaml：既存volumeのRO属性監査だけ。
- backend/tests/test_saved_snapshot_flow.py：方式Cの製品接続合成試験。
- backend/tests/test_stage10b_preflight.py・test_stage10b_postgres.py：承認制ネイティブ試験。

## 2. 安全な転送

転送自体の承認を得てから、新しい空の受信領域を/home/linux内へ作る。既存パスを使わず、既存SSHホスト鍵と照合する。失敗時に自動再送しない。

```powershell
scp -P 2222 -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o BatchMode=yes frontend/artifacts/stage10c-handoff-02.zip linux@127.0.0.1:NEW_APPROVED_INBOX/
scp -P 2222 -o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o BatchMode=yes labs/team-development/unpack_handoff.py linux@127.0.0.1:NEW_APPROVED_INBOX/
```

NEW_APPROVED_INBOXは管理者が存在を確認した新規絶対パスに置換する。既存のknown_hostsを消さず、鍵変更時は停止する。Linuxでumask077を設定し、次を実行する。

```sh
python3 unpack_handoff.py --archive stage10c-handoff-02.zip --destination NEW_NONEXISTENT_SOURCE_DIR
```

内容ハッシュ・件数・上限・パストラバーサルを検査してから新規領域へ展開する。既存先には展開しない。製品ソースはimage内の非rootから読める0644／0755、展開先rootは0700。秘密設定は別途0600／0700に作り、ソースarchiveへ混ぜない。

## 3. imageとresource

code-serverは利用者確認済みの次の参照をstage10c.yamlで固定する。

`codercom/code-server:4.145.2@sha256:aaad9c5f5ecb70713188e8466060f186b08bd328cd3e1bcffc4a2a2dca33c14b`

backendは転送済みbackendをcontextとしてbuildし、生成されたimage IDをTEAM_BACKEND_IMAGEへ固定する。ローカルbuildのimage IDとregistry manifest digestは別である。PostgreSQL・gatewayのimageは未取得なので、現在のminor版・digestを推測で記載しない。既存設計のPostgreSQL16系と非root nginxを承認後に取得し、RepoDigestsで固定してから使う。タグだけのまま起動しない。

```sh
docker build -t stage10c-backend-local ./backend
docker image inspect --format '{{.Id}}' stage10c-backend-local
# 承認されたPostgreSQL／gateway imageを取得した後だけ実施する。
docker image inspect --format '{{json .RepoDigests}}' APPROVED_POSTGRES_IMAGE
docker image inspect --format '{{json .RepoDigests}}' APPROVED_GATEWAY_IMAGE
```

image ID・RepoDigest・実版・取得日時を新規の配置manifestへ記録する。秘密を含むConfig.Envやinspect全体を出力しない。別途、固定code-server imageで/usr/bin/code-serverが実行可能な通常の入口か確認し、存在しなければlauncherを独自修正せず停止してCodexへ返す。--versionは設定作成の副作用があるため使わない。

4 vCPU・利用可能RAM約5.5GiB・空きdisk約38GiB。stage10c.yamlはeditor各1536MiB/0.75CPU、API384MiB、DB512MiB、各管理worker192MiB、gateway各128MiBへ制限する。上限合計は余裕が小さいため、build/pullと試験を段階実施し、メモリ圧迫・soft lockupの兆候があれば新規起動を停止する。既存環境を勝手に停止・削除して容量を確保しない。

## 4. UID/GID・volume・保存権限

共有されたmapはnamespace UID/GID 0→host1000、1～65536→host100000～165535。従ってnamespace1000→host100999、1001→101000、2000→101999となる。DB利用者IDとOS UIDを混同しない。既存A/Bの双方1000は双方host100999に対応し、UIDだけでは学生分離されない。

新規構成は例としてA1000、B1001、manager2000等を別々に割り当てる。値は合成配置用であり本番固定値ではない。subuid/subgid範囲と実mount所有者を確認する。ホストlinux1000で作った0700ファイルはcontainer UID0の所有となり、worker1000からは読めないので注意する。

新規領域だけを、対象のrootless namespace内で各サービスUID所有へ準備する。ホスト既存volumeへのchownを指示しない。準備のためにrootless内UID0を使う場合も新規専用領域だけ、networkなし・必要最小限のCHOWNに限定し、通常サービスは非rootに戻す。既存ファイルが一つでもあれば上書きしない。

配置承認後、NEWROOTとTOOLSを新規領域・転送検証済みlabs/team-developmentの絶対パスへ固定する。NEWROOTの不存在を確認して一度だけmkdir -m700する。次は共有mapと異なる環境や空でない領域では拒否する。合成用のID例であり、既存コンテナのIDは変更しない。

```sh
docker run --rm --network none --read-only --user 0:0 --cap-drop ALL --cap-add CHOWN \
  --mount type=bind,src="$NEWROOT",dst=/stage10 \
  --mount type=bind,src="$TOOLS",dst=/tools,readonly \
  "$TEAM_BACKEND_IMAGE" python -B /tools/prepare_layout.py --approved-synthetic --root /stage10 \
  --a 1000 --b 1001 --manager 2000 --gateway 3000 --db 4000
```

layout.jsonの各相対ディレクトリをCompose変数へ対応させる。例：WORKSPACE_A_ROOT=$NEWROOT/a/root、WORKSPACE_A_SETTINGS=$NEWROOT/a/config、WORKSPACE_A_KEYS=$NEWROOT/a/keys、WORKSPACE_A_SOCKET_DIR=$NEWROOT/a/socket。collectorの設定・鍵・台帳・UDSはそれぞれa/collector-config・collector-keys・collector-ledger・collector-socket、Bも同様。manager/config・keys・client-ledgers・git、gateway/config・tls、db/data・secretは対応サービスだけにmountする。MEMBER_A_WORKSPACE/Bはrootディレクトリそのものではなく、初期化後に確定するw-UUIDである。

学生にはw-UUID一つと専用editor-data/configだけをmountする。初期化workerは専用managed rootをRW、収集workerは同じw-UUIDだけをRO mount。Git・DB・鍵・UDS・台帳・Docker socketを学生へ渡さない。管理側・A・Bのnetworkを分け、code-serverに公開portを追加しない。既存18081/18082・28081/28082は管理gatewayを迂回するので、新構成の安全性をその接続経路で証明しない。

実code-serverで次を確認する。tmpfsで600になったという過去結果で代用しない。

1. 新規管理領域で合成ファイルを新規作成・保存し、owner UID/GIDとmode600または0644、親のgroup/other書込み禁止を確認する。
2. 同じファイルを再編集・保存し、権限が維持されることを確認する。
3. renameを伴う保存、別名保存も確認する。666へ戻る場合は拒否して原因を調べ、収集条件を緩めない。
4. 既存volumeの666はRO監査でREFUSEDを期待し、変更しない。保存後にchmodで書込みを広げた場合も拒否する。

## 5. UDS・鍵・TLS・台帳

pathname AF_UNIX/SOCK_STREAMを使用する。初期化はworker内/run/workspace/initialize.sock、管理側/run/workspace-aまたはb/initialize.sock。収集はworker内/run/collector/collect.sock、管理側/run/collector-aまたはb/collect.sock。

socket親は対応worker所有0770、socket0660。管理APIにだけ接続用補助groupを与える。サーバーは接続元primary UID/GIDをSO_PEERCREDで照合し、クライアントも相手UIDとsocket同一性を検証する。TCPへfallbackしない。不明な既存socketをunlinkしない。

各A/B・初期化/収集で、request、response、authorizationの3種類の鍵を別々に作る。両端で同じ用途の鍵を安全に複製するが、用途間では使い回さない。鍵をURL、JSON本文、ログ、環境変数全体へ出さない。

同じコードを通常ユーザーで動かす具体例は次のとおり。他の用途もsrc・UID/GID・kindをlayout.jsonに合わせて明示指定する。既存のprivate領域を使わない。

```sh
docker run --rm --network none --read-only --cap-drop ALL --user 1000:1000 \
  --mount type=bind,src="$NEWROOT/a/collector-keys",dst=/private \
  --mount type=bind,src="$TOOLS",dst=/run/managed,readonly \
  "$TEAM_BACKEND_IMAGE" python -B /run/managed/provision_private.py \
  --approved-synthetic --directory /private --kind keys
```

管理API側の同用途コピーは、新規manager/keys内の対象ファイルの不存在を確認してから、rootless内の限定した準備用処理でinstall -m600 -o2000 -g2000を使う。例：a/collector-keys/request.key→manager/keys/a-collect-request.key。response・authorization、Bも同じ規則。初期化用のa/keysは設定例に合わせてa-request.key等へ複製する。準備処理には新規NEWROOTだけを渡し、必要なCHOWN/DAC_OVERRIDEを限定付与する。値をcat・echoせず、同一性は内部バイト比較の真偽だけで確認する。ファイルが存在する場合はinstallで上書きしない。

対象UIDが所有する空の0700領域を用意し、同じUIDの管理imageで次を実行する。固定の/app import先を使用する。各用途は別の空領域を指定する。

```sh
python -B /run/managed/provision_private.py --approved-synthetic --directory NEW_PRIVATE_KEYS --kind keys
python -B /run/managed/provision_private.py --approved-synthetic --directory NEW_WORKSPACE_LEDGER --kind workspace-ledger
python -B /run/managed/provision_private.py --approved-synthetic --directory NEW_REPLAY_LEDGER --kind replay-ledger
python -B /run/managed/provision_private.py --approved-synthetic --directory NEW_GATEWAY_TLS --kind tls
python -B /run/managed/provision_private.py --approved-synthetic --directory NEW_DB_SECRET --kind database-password
```

既存ファイル・所有者不一致は拒否する。生成されたinitialization.sqliteを初期化設定へ、requests.sqliteを対応する収集台帳またはclient_ledgerへ指定する。台帳の同じファイルを両端で共有しない。TLSは7日間有効・127.0.0.1/localhost用の合成自己署名証明書で、本番CAとして信頼しない。公開証明書とfingerprintを別経路で確認し、システム全体の証明書検証を無効にしない。

DB用passwordはDB UIDのdb/secretだけへ生成し、TEAM_DATABASE_PASSWORD_FILEに固定する。APIと試験のDB接続設定は同じ新規合成DB・資格情報を管理者が私有設定へ束縛する。接続URLを端末へ表示せず、通常Composeのconfigは--quietだけを使う。実DB・実ユーザーの既存passwordは再利用しない。

client台帳はmanager/client-ledgers/a/requests.sqliteとb/requests.sqliteに別々に作る。gateway設定は次の生成器を使い、nginxの$http_cookie等を独自のenvsubstで消さない。元の環境変数指定CLIも維持している。

```sh
python -B render_gateway.py --a INITIALIZED_A_WORKSPACE_UUID --b INITIALIZED_B_WORKSPACE_UUID --output NEW_PRIVATE_NGINX_CONF
```

## 6. Landlockと強制期限

製品rulesetはworker_sandbox.pyそのものを使用する。ABI8であることに加え、EXECUTE/WRITE/REMOVE/MAKE/REFER/TRUNCATEまでの既存処理対象を制限する。収集・検証は固定rootのREAD_FILE/READ_DIRと/proc/self/fdinfoの読取りだけ。初期化だけは新規rootに必要な通常ファイル／directory作成・書込みを許可する。未知のkernel対応で弱い代替にしない。

親側の期限、terminate→kill→終了確認、親死亡時SIGKILL、子孫生成禁止を試験する。終了・結果が不明ならUNKNOWNと受付を保持する。同じ提出IDで自動再収集しない。ワーカーにGit・DB・認証鍵を渡さず、不完全フレームをGitへ登録しない。

## 7. 設定・起動・検証・停止

settings-stage10.example.jsonとsettings-snapshot.example.jsonを管理UID所有0600のsettings.jsonへ分割する。初期化後に返ったworkspace UUID、DBの基準、w-UUIDの実パスを照合し、その値だけをcollector/gatewayへ登録する。任意パスやOIDを利用者から受け取らない。collectorsは固定設定を明示注入し、通常mainのprovider拒否は維持する。

gateway_enabledは初期false。TLS・固定route・直接アクセス遮断・cookie検査を確認し、別途承認後にtrueとする。APIは既存Firebase認証を維持するため、今回禁止されている実Firebaseへ接続して試験しない。認証付き主要フローは既存の合成API結合試験で検証する。通常画面で実Firebaseへログインした主要フローを確認済みとは扱わない。

```sh
docker compose -p stage10c-isolated -f compose.yaml -f stage10.yaml -f stage10c.yaml --profile stage10-approved --profile stage10-isolated config --quiet
# 設定検査・所有者・鍵・新規DBの確認と個別起動承認後だけ、対象を限定する。
docker compose -p stage10c-isolated -f compose.yaml -f stage10.yaml -f stage10c.yaml --profile stage10-approved --profile stage10-isolated up -d --no-deps db
# migration試験の完了後、同じ形式でworkspace-a/b、backend、collector-a/b、
# session-gateway、member-a/b、gatewayを必要順に個別指定して起動する。
```

自動的な全サービスupはしない。collectorのrootは初期化後に確定するため、先に仮のパスで起動しない。Composeが変数解決のため全パスを必要とする場合も、既存領域で穴埋めしない。初回は初期化専用構成の設定検査から行う。

PostgreSQLは新規stage10_synthetic_で始まるDBだけを使う。PROJECT_TEST_DATABASE_URLは秘密設定から渡し、echoしない。次は既存schemaを触らずランダムschemaで旧基本テーブルを用意し、005～011を適用する。

```sh
STAGE10_SYNTHETIC_POSTGRES=approved DATABASE_URL=sqlite:// python -B -m unittest discover -s tests -p test_stage10b_postgres.py
STAGE10_SYNTHETIC_NATIVE=approved STAGE10_SYNTHETIC_ROOT=NEW_PRIVATE_TEST_ROOT DATABASE_URL=sqlite:// python -B -m unittest discover -s tests -p test_stage10b_preflight.py
env -u PROJECT_TEST_DATABASE_URL DATABASE_URL=sqlite:// python -B -m unittest discover -s tests -p test_saved_snapshot_flow.py
env -u PROJECT_TEST_DATABASE_URL DATABASE_URL=sqlite:// python -B -m unittest discover -s tests -p test_submission_rpc_provider.py
```

全migrationを実証する試験を、アプリ起動時のcreate_allで代用しない。新しい合成運用DBへの005～011適用は、既存の基本テーブル・002～004の状態を照合してから同じ順序で行う。既存DBへ流さない。

方式Cの上記API結合試験はSQLiteと一時Gitであり、PostgreSQL試験へ読み替えない。PROJECT_TEST_DATABASE_URLを外さずにこのSQLite lifecycle fixtureへ流用しない。PostgreSQLについて今回用意した実証は005～011の移行と同一requestの競合で、API全体の実PostgreSQL結合は別途実施が必要。

画面デモはWindowsのfrontendでnpm run build:demoを実行し、生成したartifacts/stage9-demoをローカル静的serverで開く。これは明示した合成APIであり、通常画面の失敗時fallbackではない。実code-server・DB・Firebase接続済みという表示はしない。通常のビルド成果物やnode_modulesはこの転送ZIPへ含めない。

| 試験 | 合格条件 |
|---|---|
| 666・所有者・リンク・mount境界 | 不正なものを収集前に拒否、Git参照が増えない |
| 初期化と再起動 | 同じIDは一回受付、UNKNOWNを再初期化しない |
| 強制期限・途中切断 | 完全な結果以外をGit保存しない、台帳が消費済み |
| 確認後にファイル編集 | 提出commitは確認済み内容のまま、収集回数が増えない |
| 失効・他メンバー・確認ID転用 | 新規ChangeRequestを作らず拒否 |
| 二重提出・応答喪失 | 同じ依頼IDを照会でき、重複依頼・自動再収集なし |
| レビュー・反映 | 自己承認拒否、別メンバー承認、mainとbase不一致時は統合拒否 |
| WebSocket失効 | 新規接続拒否と継続接続切断。通信不能でも継続許可しない |
| PostgreSQL競合 | 一要求一受付、有限timeout内に完了または明示エラー、確定状態を降格しない |

終了は今回起動した新規service名だけにstopを使う。既存デモには実施しない。例：同じcompose引数でstop collector-a。対象PID・project名・service名を確認できなければ操作しない。down -v、prune、台帳削除、socket自動削除は使用しない。

鍵・DB接続先・UID対応・固定imageが不明、666への逆戻り、Landlock失敗、セッション転用、証拠不一致、タイムアウト後の終了不明、予期しない再収集、デッドロック、soft lockupの兆候があれば停止して結果だけを返す。コードや設定をCopilotが独自改修せず、Codexへ差し戻す。
