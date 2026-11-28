# Stage 10-D：合成認証を用いた実機MVPの引き渡し

この手順の展開・配置・起動はCopilotが別途承認後に実行する。Codexの引き渡し作業はcommit・push・新規受信領域への転送までで、サービスを起動しない。実施したcommitと転送ハッシュは付属の `stage10d-release-01.json` と作業報告を参照する。runcは合成専用であり、実利用者へ公開しない。PR #21・#23、既存A/Bデモ、旧volume、旧DB data、旧source、旧layoutを削除・上書きしない。

## 入口と信頼境界

- 通常入口 `app.main:app` はFirebase初期化を維持する。通常起動から合成認証へのfallbackはない。
- 合成実機入口は `app.synthetic_asgi:app`。Linux非root、明示承認、専用PostgreSQL名、専用fixture marker、管理UID所有のprivate設定をすべて要求する。Firebaseが既に読み込まれていても拒否する。
- 共通の製品routeは `app/application_routes.py` に一度だけ定義する。認証以外の認可、DB、Git、固定スナップショット、レビュー、承認、統合を置換しない。
- `synthetic_auth.py` はA/B別PBKDF2資格情報、サーバー生成opaque token、digestだけのSQLite保存、15分期限、永続的失効、試行制限を実装する。UID入力を資格情報にしない。
- 編集セッションは `synthetic_application.bound_sessions` でログイン台帳へ結合する。ログアウト・期限切れ後はcookieによる新規接続と継続WebSocketのgate照会も拒否する。DB上の所属・担当・作業基準・失効・方式の検証も維持する。dirty=falseは生成しない。
- 合成画面は `frontend/stage10d-native.html`。実APIへ接続する。既存のmemory APIデモとは別buildであり、Firebase SDK、API偽装、失敗時の架空データは使わない。
- code-server 4.145.2の既定Cookie名は、[対象版の公式ソース](https://github.com/coder/code-server/blob/v4.145.2/src/common/http.ts)で確認した `code-server-session`。今回の生成設定ではcookie-suffixを付けない。WebSocket gatewayはこの名前だけを転送し、管理側のwork_editor Cookieを転送対象にしない。実接続の成功は別途ブラウザー試験で確認する。

## 成果物とcommitの対応

正式ZIPは `frontend/artifacts/stage10d-release-01.zip`。以前の `stage10d-handoff-final-01.zip` と途中候補01・02は保全するが、引き渡し対象にしない。正式ZIPはcommit後に生成し、201ソースファイルすべてを確定commitと照合する。`stage10-manifest.json` は全ファイルのバイトSHA-256、Git属性と明示的なcore.autocrlf=trueで正規化したblob ID、対象HEAD、`COMMITTED` 状態を持つ。WindowsとLinuxの改行設定差を照合時に吸収する。

Windowsで確定commitから正式ZIPを作り、信頼済みのローカルGitで次を実行する。ZIP自身に書かれたcommit IDを信用して比較先を選ばない。照合結果・対象commit・ZIPのSHA-256は付属のrelease JSONへ記録する。JSON自体は署名ではなく、既存SSHホスト鍵の厳格照合で保護された転送と、作業報告の値も照合する。

```sh
python3 labs/team-development/verify_handoff_commit.py \
  --archive NEW_ARCHIVE --repository TRUSTED_LOCAL_REPOSITORY --trusted-commit APPROVED_FULL_COMMIT
```

全対象ファイルのハッシュとGit blobが指定commitに一致した場合だけ `COMMIT_MATCH` となる。転送承認後、既存SSHホスト鍵の厳格照合、接続・実行期限、新規受信領域、転送前後のSHA-256比較を使う。失敗時の自動再送は禁止。展開は同梱 `unpack_handoff.py` で**不存在の新規先**へ行う。旧173ファイルの展開先は上書きしない。

Ubuntuでは、まず受信したZIP・手順書・展開器・manifestのSHA-256を作業報告／release JSONと照合する。ZIPの `stage10-manifest.json` と別送manifestが同一であることも確認する。その後だけ次を実行する。信頼済みGitがUbuntuにもある場合は、上記commit照合も実行できる。ない場合もWindowsで実施済みの201件照合結果と認証済み転送を確認し、commit IDだけを信頼根拠にしない。

```sh
python3 unpack_handoff.py --archive stage10d-release-01.zip --destination NEW_NONEXISTENT_STAGE10D_SOURCE
```

展開後の `docs/stage10d-handoff.md` が正本である。新規source内の配置ツールを使用し、以前のsourceへコピーしない。

## 既存Linux環境の取り扱い

共有済みの174 PASS／1 SKIP、実Landlock・UDS・SO_PEERCRED・HMAC v2、PostgreSQL 001～011成功は前工程の証拠であり、今回の新入口の成功ではない。

旧layout 37領域は、rootless準備namespace内で `audit_layout.py --root /stage10` を実行して所有者・mode・リンクを読み取り確認できる。成功表示は `METADATA_MATCH_ONLY` で、再利用許可ではない。旧鍵・台帳・DB marker・Git・設定の由来が不明、部分初期化あり、設定が既存の場合は、新しいlayoutと新しい合成DB/Git/vaultを選ぶ。旧領域を空にしたり修復したりしない。

停止済み既存PostgreSQLのdataは保持する。既存DBにfixtureを追記しない。fixtureは空の専用DBだけに作る。新規DBを同じ承認済み合成PostgreSQLインスタンスに作るか、新規data領域のインスタンスを使うかは、環境側で承認して記録する。

rootless mapはnamespace 0→host1000、1→host100000から65536個。非zero namespace UIDのhost値は `99999 + namespace_UID`。A/B/manager/gateway/dbは異なるUID/GIDを割り当てる。DBのメンバーIDとは別。既存volumeのchmod/chownは禁止する。

## imageとbuild完了

- code-server: `codercom/code-server:4.145.2@sha256:aaad9c5f5ecb70713188e8466060f186b08bd328cd3e1bcffc4a2a2dca33c14b`
- PostgreSQL: `postgres:16.16-alpine@sha256:9d9e487c1bd6413c52ccb3c02f46c8e37fa1e1d1a32a84519eb4e68d0ad1da30`
- backend base: 管理者が取得・確認した `python:3.11-slim...@sha256:...` を必須引数にする。確認できていないdigestは捏造しない。
- gateway: 管理者が承認したnginxの固定RepoDigestを `STAGE10_GATEWAY_IMAGE` と `TEAM_GATEWAY_IMAGE` に同じ値で指定する。TLS、auth_request module、非root起動対応を確認する。未確認の可変タグで起動しない。

```sh
python3 labs/team-development/build_image.py --base "$PINNED_PYTHON_BASE" --receipt "$NEW_BUILD_RECEIPT"
```

`Dockerfile.stage10d` を使用し、build終了0、新規iidfile、image inspect一致が揃って初めて成功。出力image IDを `TEAM_BACKEND_IMAGE` に固定する。以前のimageが存在しても、export時 `context canceled` は成功ではない。旧imageを削除しない。build失敗時の生ログを共有せず、秘密値を除いた原因を確認する。

## 配置・起動の実行順序

以下のコマンドは承認済みの新規合成領域にだけ適用する。`docs/stage10c-handoff.md` のrootless準備、用途別鍵の分離・配送、UDS、TLS、台帳provision、マイグレーション、RO mount手順を再利用する。製品コードの手編集は不要。設定生成器の出力を使い、旧説明用複合JSONをASGIへ直接渡さない。

1. 新規sourceを検証・展開し、`npm ci`（必要時）と `npm run build:native` を実行する。公開mountは `frontend/artifacts/stage10d-native`。通常 `dist` やmemoryデモを混在させない。
2. 新規／再利用承認済みlayoutのmetadataを照合する。新規の合成auth vault（manager UID所有0700）と設定bundle領域を別に確保する。用途別鍵と各SQLite台帳は既存 `provision_private.py` で不存在先にだけ作る。workerとmanagerへ配送する同用途の鍵の一致を値を出さず照合する。初期化・収集・応答・認可・A/Bの用途を共有しない。
3. 新規合成PostgreSQLへ001～011を順番に適用する。専用DB名は `stage10_synthetic_...`、コンテナ内host名は `db` または `stage10-db`。DB資格情報はmanager/dbだけに渡し、環境全体やCompose展開全文をログへ出さない。
4. manager UIDで、DBと新規Git rootをmountした一回限りの管理コンテナから実行する。vault内にADMITTEDが残ったら再実行しない。

   ```sh
   STAGE10_SYNTHETIC_APPROVED=1 python -B -m app.synthetic_fixture \
     --private-directory /run/synthetic-auth --git-root /var/lib/team-projects
   ```

   `accounts.json`（hash）、`auth.sqlite`、`fixture.json`、管理者だけが読む `operator-credentials.json` を0600で作る。A/BのUser、Team、Project、担当Task、登録対象、共有Git基準を作る。専用marker表が1個追加されるため、通常22表から合成fixture環境では23表となる。通常DB用マイグレーションの変更ではない。元のDBに行があれば拒否する。
5. rootless内の5役割のUIDを記録したprivate `ids.json` を作る。以下の生成器を明示承認フラグ付きで実行する。秘密値・設定全文を表示しない。

   ```sh
   STAGE10_SYNTHETIC_APPROVED=1 PYTHONPATH=/app python -B /tools/native_settings.py \
     --fixture /run/synthetic-auth/fixture.json --ids /run/private/ids.json --output /run/private/bundle-prepare
   ```

   初期化前のbundleはeditor経路を持たない準備用nginxと、正しいASGI schemaを生成する。`install_native_configs.py --layout /stage10 --bundle /bundle --phase prepare` をrootless準備namespaceで実行すると、新規 `stage10d-prepare` 設定親だけに設置する。読み取り・新規ファイル所有者設定に必要なCHOWN/DAC_OVERRIDEだけをこの一回限りの準備コンテナへ付与し、常駐サービスには付与しない。既存先があれば拒否する。
6. Composeは `compose.yaml` → `stage10.yaml` → `stage10c.yaml` → `stage10d.yaml` の順。Compose 2.24.4以上が必要。`!override` でbackendのFirebase credential mountを除去する。baseファイルの補間に必要な `TEAM_FIREBASE_CREDENTIAL_FILE` には非実在の拒否用sentinel `/REFUSED-NO-FIREBASE` を指定する。**最終構成にこのmountが残る場合は起動しない**。`docker compose ... config --quiet` で構文を確認し、資格情報を伏せたmount・image・port・userだけを別途確認する。
7. 各設定変数は生成された親を指定する。準備時は `WORKSPACE_BACKEND_SETTINGS=.../manager/config/stage10d-prepare`、workspace A/B settingsは各 `config/stage10d-prepare`、gateway configは `gateway/config/stage10d-prepare/nginx.conf`。`SYNTHETIC_AUTH_ROOT` はvault。`TEAM_TLS_PORT=28443`、originは **`https://localhost:28443`** に統一する。TLSは新規テスト証明書、ブラウザーでfingerprintを確認してこのoriginだけを信頼する。A/Bの本製品認証とcode-serverの個別passwordは別用途。
8. 初期化前はdb・workspace-a/b・backend・gatewayだけを明示起動する。Compose変数の未初期化workspace欄は新規専用rootのplaceholderとし、member/collector/session-gatewayを起動しない。公開はloopback TLS gatewayだけ。ブラウザーでA/Bそれぞれログインし、自分の担当Taskから「作業環境を準備」を実行する。
9. managerの一回限りの管理コンテナで、準備済みbasisを**読取り専用**で出力する。

   ```sh
   STAGE10_SYNTHETIC_APPROVED=1 python -B -m app.synthetic_bases \
     --fixture /run/synthetic-auth/fixture.json --output /run/synthetic-auth/bases.json
   STAGE10_SYNTHETIC_APPROVED=1 PYTHONPATH=/app python -B /tools/native_settings.py \
     --fixture /run/synthetic-auth/fixture.json --ids /run/private/ids.json \
     --bases /run/synthetic-auth/bases.json --output /run/private/bundle-connected
   ```

10. `install_native_configs.py ... --phase connected` で新規 `stage10d-connected` 親へ設置する。設定mount先をconnected親に変更し、新構成のbackend/gatewayだけを対象限定で止めて再作成する。これはLinux側の別途承認後の操作であり、旧A/Bは操作しない。workspace設定の内容は同じでも旧設定ファイルを上書きしない。
11. `MEMBER_A_WORKSPACE=$NEWROOT/a/root/w-<Aのworkspace UUID>`、Bも同様。UUIDはDB exportから採用し、パス存在・owner・mode・リンク・mount IDを確認する。w-UUIDだけをeditorへRW、collectorへRO。collector親root、Git、DB、Docker socket、鍵・台帳はeditorへ渡さない。
12. collector A/B、session-gateway、member A/Bを明示起動する。全起動・停止コマンドに同じ新規Compose project名・4設定ファイル・明示service名を指定する。停止は当該サービスだけの `stop` を使い、`down -v`、prune、旧コンテナ操作は行わない。

## ブラウザーで確認する合否基準

既存のLinux 174 PASS／1 SKIPを最初からすべて繰り返すより、新規の合成ASGI・fixture・設定schema・認証付きgateway、実保存mode、セッション失効、新規DB/Git競合試験を優先する。変更のないLandlock・UDS・SO_PEERCRED・HMAC v2・リンク拒否も、新image／新設定で最低限の正常・拒否smokeを行う。既存証拠を再利用する場合はソースblob・image・kernel・mount・設定の対応を確認し、差があれば該当試験を追加する。

1. AとBは別ブラウザープロファイルでログインする。AのpasswordでBとして認証できない。未認証・他メンバーのworkspace・他Taskは401/403。合成環境のbannerが常に見える。
2. Aがcode-serverで新規作成、既存demo.txtの編集、再保存、別名保存を行う。本文は報告せず、保存後のUID/GID・mode・リンク数だけを確認する。600/644等のgroup/other書込み禁止、単一リンクを要求する。新規管理領域のlauncherはumask077。666へ戻る場合は停止し、収集器の条件を緩めない。既存666 volumeを修復しない。
3. 保存済み内容を収集・固定して一覧と差分を確認する。その後の追加編集・保存や未保存バッファが、確認済みスナップショットへ混入しない。明示提出前にChangeRequestは作られず、提出時に再収集しない。
4. Bが既存ChangeRequestの差分を確認し、承認／修正依頼、承認後の共有コードへの反映を実行する。自己承認不可・権限維持・main/base不一致時の拒否を確認する。Taskを自動完了しない。
5. Bの最新版取得は新世代を作る。未保存バッファを取得せず、保存済み未提出変更があれば拒否し、旧workspaceを残す。自動配備が未接続なら新しいw-UUIDへのmount・固定gateway routeの再生成は管理者が対象限定で行う。旧世代を上書きしない。
6. code-serverの実WebSocket接続中に合成ログアウト、DB編集セッション失効、期限切れ、Team所属解除を行い、gate拒否と継続接続切断を実証する。設定上の監視間隔だけで切断成功とは扱わない。直接公開portによる迂回があれば不合格。
7. 通信断後は既存request IDのGET結果照会のみ。自動再収集・別IDでの再提出なし。台帳再起動後の再利用拒否、強制期限後のworker終了／UNKNOWN、Landlock、SO_PEERCRED、UID/GID不一致、HMAC改変、RO mount、symlink/hardlink/bind mount拒否を再実証する。
8. PostgreSQL/Git実競合は `test_stage10b_postgres.py` の新規 `test_real_db_git_opposite_order_exits_without_deadlock` と既存並行・復旧suiteを専用DBで実行する。実PostgreSQL行ロックと実Gitロックを別threadで逆順に取得し、Git非待機拒否後のDB解放で両処理が終了することを要求する。Windowsではこの新規native試験はSKIPであり、実証は未完了。

管理コンテナ内（/app）で、専用DB URLを秘密設定から環境へ注入した後だけ実行する。URLや環境全体を表示しない。

```sh
STAGE10_SYNTHETIC_POSTGRES=approved python -B -m unittest discover -s tests -p test_stage10b_postgres.py
```

全Compose操作の対象を固定するため、承認されたshellで次の関数を使える。未設定の変数は手順で確定し、既存project名と重複させない。

```sh
C() {
  docker compose --project-name stage10d-synthetic-mvp --env-file "$PRIVATE_ENV" \
    -f "$TOOLS/compose.yaml" -f "$TOOLS/stage10.yaml" -f "$TOOLS/stage10c.yaml" -f "$TOOLS/stage10d.yaml" \
    --profile stage10-approved --profile stage10-isolated "$@"
}
C config --quiet
# prepare設定を設置後だけ
C up -d db workspace-a workspace-b backend gateway
# connected設定・実workspaceのmountを確定後だけ（旧A/Bではない）
C stop backend gateway
C up -d backend gateway collector-a collector-b session-gateway member-a member-b
# 検証終了後、今回のサービスだけを停止。volumeを消さない。
C stop member-a member-b collector-a collector-b session-gateway gateway backend workspace-a workspace-b db
```

## 停止条件と残課題

資格情報の漏出、他メンバー領域への到達、旧領域変更、666保存、直接接続迂回、失効後の接続継続、台帳異常、証拠不一致、build非0、image未固定、unknown結果の再実行、soft lockup／メモリ圧迫の兆候で停止する。既存データを削除して解消しない。

今回のWindows検証は新しいLinux配置の成功ではない。新しいimage、nginxの実版/digest、rootless設定設置、PostgreSQL fixture、実TLS/WebSocketとブラウザー保存権限、実DB/Git競合の実証が残る。A2未完了、B1未承認、SSH255原因分類Hを維持する。
