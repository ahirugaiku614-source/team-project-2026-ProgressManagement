# Stage 10-A：実接続アダプターと拒否条件

## 到達点と利用制限

初期化RPC、初期化の永続受付、継続WebSocket失効監視、収集の独立プロセス実行を追加した。通常のmainはこれらを自動登録しない。実provider、実編集セッション検証器は拒否のまま。実利用者による編集から提出までが利用可能になったという意味ではない。

実エディターの未保存状態を管理側で独立して証明するauthorityは未成立である。利用者と同一権限で動く拡張の報告や、単なるセッションIDを証拠にしない。IndependentEditorAuthorityは常に拒否し、専用bootstrapもeditor_gateway=Falseを維持する。初期化の可否と編集・提出の可否を分離した。

## 初期化の処理順序

1. Firebaseと既存Team・Project・Task認可を使用し、管理側でProjectの基準を固定する。
2. WorkEnvironmentOperationへ作業基準ID・基準commitを条件付きで記録し、遠隔副作用より先にcommitする。
3. WorkspaceAuthorizationIssuerが現在の担当・登録対象と操作記録を確認する。通信HMACとは異なる認可鍵で、全要求を署名する。
4. 固定UDSの相手UID・socket安全性、通信HMAC、用途別認可、20秒以内の期限、固定対象を検証する。任意パス・URL・コマンドを受け付けない。
5. WorkspaceJournalへACCEPTEDを原子的に記録してから独立ワーカーを起動する。登録済み対象と基準スナップショットから新規領域のみ生成する。
6. 完全な初期化証拠を得た場合だけCOMPLETEにする。途中失敗はUNKNOWN。同じ受付から自動再初期化しない。

通信応答が失われた場合、既存request IDの照会はLOOKUPと証拠検査だけを行う。作業基準やGit参照を自動再生成せず、DBのCONFIRMEDを推測しない。UNKNOWN・記録欠落は未実行の証明にならない。手動で証拠を確認するまで新規IDで再試行しない。

010_workspace_initialization_rpc.sqlは操作記録のbase_commitを追加する。実DBには未適用。以前の005～009との順序と実PostgreSQLの移行検証が必要。

## プロセス期限とファイル境界

ManagedProcessは固定のmanaged_worker.pyだけを起動する。子へDB資格情報・Git書込み権限・認証鍵・通常の環境変数を渡さない。利用者コード、Gitフック、ビルドは実行しない。子のDB依存も分離した。

親が期限を管理し、terminate、短い待機、kill、終了確認を行う。終了確認不能・応答不完全・異常終了はWorkerUnknownとなり、そのrunnerを隔離する。同一提出IDを再収集しない。永続受付は残り、完全なフレームで返った収集結果だけを後段へ渡す。管理者が不明なプロセスと台帳を調査するまで再開しない。

実ワーカーはLinux x86_64・非root・Landlock ABI 3以上を必須とする。NO_NEW_PRIVS、親死亡時SIGKILL、コア出力禁止、子孫生成制限を設定する。収集元は読み取り専用mountを要求し、FD相対走査、所有者、リンク、ファイル同一性、二回照合、容量・件数・深さ制限を維持する。/proc/self/fdinfoによるmount ID照合も必要。対応しない環境で弱い処理へ切り替えない。

Landlockはネットワーク全体の隔離を証明するものではない。固定の管理コードだけを実行する契約に加え、収集コンテナのnetwork_mode:noneを維持する。Linuxの実シグナル、FD、mount、Landlock、親死亡、孤児化防止はWindows試験では未検証。

## 継続接続の失効

専用SocketGatewayは固定workspaceと内部code-serverの対応だけを使用する。HTTPS originを照合し、対応するwork_editor cookieのみを固定FastAPIの読取りgateへ送る。最初と継続中に期限・失効を照会し、拒否・確認不能・通信断でWebSocketを閉じる。新規接続だけを確認するnginx auth_requestとは役割を分ける。

標準の監視間隔は0.5秒、照会上限は1秒。ハードリアルタイムの切断保証ではない。実Linuxで遅延・通信断・同時接続を確認する。cookie・本文・認証情報のアクセスログを出さない。code-server固有cookieを必要とする場合は管理設定の明示allowlistだけを転送する。正しいcookie名とパス、WebSocket互換性は実版で未確認。

code-serverに公開portを設定せず、管理されたgatewayだけを経由させる。gateway以外の接続元をネットワーク境界でも拒否する。設定例の内部HTTPは専用ネットワークを信頼する配置案であり、別ホストではそのまま使用しない。

## dirty authorityに必要な設計判断

利用者が変更できる拡張や、同じUIDのterminal・debuggerから変更できるエディターprocessを独立した証明者にできない。拡張をread-onlyにするだけでも不十分。別ウィンドウや切断中ブラウザー内の未送信バッファまでサーバーだけで証明することもできない。

実接続前に、保証する編集範囲を定義し、管理されたエディター本体・独立agent、利用者terminalとのUID/PID/debug隔離、全ウィンドウ登録、heartbeatと失効、検査と収集の競合対策を設計・承認する必要がある。未登録・オフラインの編集をどう扱うかも明確にする。今回これを自己申告の代替実装で解決したとは扱わない。

## Stage 10-Bで管理者・Copilotに依頼する環境準備

- PR #21と異なる新規隔離領域、合成メンバー二人、別UID/GIDを用意する。既存領域を再利用しない。
- digest固定image、Landlock対応kernel、読み取り専用収集mount、/proc FDメタデータを確認する。
- 管理者が固定JSON設定を0600、専用親領域を0700で作成する。鍵は用途別に異なる32～64 byteを生成し、本文や報告へ出さない。
- UDSだけは専用ワーカー所有・接続用groupの0770親領域、0660 socketを使う。backendにだけA/Bの接続用補助groupを追加し、primary UID/GIDの資格情報検査と区別する。鍵・台帳・作業rootは0700を維持する。
- WorkspaceJournal.provisionを新規の専用領域へ一度だけ実施する。既存台帳を消したり作り直したりしない。UDS親・台帳・鍵の所有者と相手UIDを合わせる。
- labs/team-development/compose.yamlとstage10.yamlを合わせて事前に設定検査する。今回はCompose自体を実行していない。stage10-isolatedとstage10-approvedのprofileは明示承認後だけ使用する。
- 初期化ワーカーはapp.workspace_bootstrap、管理APIはapp.stage10_asgi、継続接続はapp.editor_socket_gatewayを明示的に起動する。通常app.mainを起動しただけでは実接続しない。
- DB移行、image作成・配置、TLS、UDS作成、mount、起動・終了はすべて別途承認を受ける。対象プロセスを限定し、不明なsocketは削除しない。

設定例はsettings-stage10.example.jsonを参照する。説明用placeholderを実際の管理設定へ置き換えずには起動できない。秘密鍵そのものをJSONへ記載しない。学生側にGit・DB・socket・鍵をmountしない。

各SETTINGS環境変数はファイルそのものではなく、settings.jsonを含む管理者所有0700ディレクトリを指定する。/run/workspace-configへread-only mountする。targetのIDとUID/GIDは別物であり、置換後は整数にする。A/Bで別の設定と鍵を使用する。

初期化後のworkspace UUIDとw-UUID実パスを照合し、管理者がcode-server側の固定mountとgateway対応を設定する。汎用Dockerソケットや任意コンテナ生成APIは追加していない。新世代の自動配備・動的なgateway設定更新は今回完了していない。起動コマンドは構成案中に記載したが、実行可能性は次工程で確認する。

## 実証項目と残課題

Linuxで初期化重複・応答喪失・再起動後の拒否、別メンバーへのアクセス拒否、実SO_PEERCRED、socket置換、mount境界、子プロセス強制終了を合成データで検証する。PostgreSQLでは移行、条件付き更新、commit応答喪失、並行処理を確認する。

新規初期化の受付commitはRPCより先に確定する。Project Gitロック下で基準を固定する既存順序を保つ。一方、従来経路を含むDB/Gitロックの全体的統一は未完了で、実PostgreSQLでの競合実証が必要。Windowsの成功をデッドロック不存在の証明にしない。

初期化証拠の照会、保存済み結果の確認、ChangeRequest作成、承認、統合は別状態のまま。保存だけでは提出しない。既存の二人合成フローは回帰対象だが、今回の実authorityが未成立なので実エディターでの一連の成功は未確認。

A2未完了、B1未承認、SSH255原因分類Hを維持する。Stage 10-Bは隔離合成環境の検証準備に限って進められる。実利用者への接続・提出有効化はauthorityの設計承認と実証後に別途判断する。

## Windows合成検証結果

最終全回帰は45モジュール・930件、765 PASS／0 FAIL／0 ERROR／165 SKIP。新規34件はすべてPASS。Stage 9の全体結果896件・731 PASS／165 SKIPから34件増加し、SKIP条件は変更していない。過去の選択suiteの328 PASS／105 SKIPとは実行範囲が異なる。

SKIP内訳はLinux固有143件、隔離PostgreSQLを必要とする21件、明示許可が必要なDocker診断1件。実DB・実Dockerには接続していない。実Linuxの成功結果ではない。

最初の全回帰ランナーは標準入力からPythonを起動していたため、Windows spawnによる既存複数プロセス試験11件が3 FAIL／8 ERRORとなった。製品・試験条件を変えずに-cでモジュールを起動し、全930件を再実行して上記の最終結果を確認した。初回結果をPASSに読み替えていない。

新規試験は製品DB認可・HMAC・永続SQLite受付・RPC・既存Git処理を接続し、OSのファイル操作部分は合成実装を明示注入した。管理用固定補助プログラムの実Windows子プロセスを使い、期限超過・部分応答・異常終了・過大応答・隔離を確認した。実FastAPIのDB失効判定と模擬WebSocketを接続した切断試験も実施した。

フロントエンド68 PASS、提出用拡張10 PASS。通常ビルド・デモ専用ビルド成功。通常ビルドの約638KBチャンク警告は残存。lint・型検査は既存コマンド未設定のため未実施。実ブラウザー・実code-server・実WebSocket upstreamの動作確認は未実施。

変更Python22ファイルのAST構文検査、git diff --check、新規ファイルの空白検査、JSON構文、YAML構文と静的配置条件の検査を実施した。Compose起動・構成コマンド実行は行っていない。models.pyの以前からある末尾空行は無関係な整形を避けて保持した。

変更候補の秘密情報パターン検査では実秘密値を検出していない。テストの固定HMAC値は合成専用。パターン検査とレビューには検出限界があり、秘密情報の絶対不存在を保証しない。

## 変更ファイル一覧（27件）

初期化RPC・DB非依存の契約・永続受付・明示bootstrap：

- backend/app/workspace_rpc.py
- backend/app/workspace_scope.py
- backend/app/workspace_journal.py
- backend/app/workspace_bootstrap.py
- backend/app/stage10_asgi.py
- backend/app/workspace_files.py
- backend/app/work_environment.py
- backend/app/task_review.py
- backend/app/task_review_api.py

期限付き実行・収集接続・UDSフレーム制限：

- backend/app/managed_process.py
- backend/app/managed_worker.py
- backend/app/worker_sandbox.py
- backend/app/submission_collector.py
- backend/app/submission_server.py
- backend/app/submission_uds.py

未成立authorityの明示拒否・継続接続失効：

- backend/app/editor_authority.py
- backend/app/editor_socket_gateway.py

DBの追加項目・未適用SQL：

- backend/app/models.py
- backend/migrations/010_workspace_initialization_rpc.sql

合成検証：

- backend/tests/test_managed_process.py
- backend/tests/test_editor_socket_gateway.py
- backend/tests/test_workspace_runtime_rpc.py
- backend/tests/test_work_environment.py

配置案と記録：

- labs/team-development/nginx.conf.template
- labs/team-development/stage10.yaml
- labs/team-development/settings-stage10.example.json
- docs/team-development-stage10a.md

既存test_work_environmentの変更は、準備用workspacesまで偽物を接続した状態を「既定」と呼ばず、実際のDeniedWorkspacesで既定拒否を検証するため。dirty検証の拒否条件は維持し、新しい試験でも明示確認した。

旧worktreeの7ファイルはバックアップと直接バイト一致、バックアップ10ファイルと保護対象3ディレクトリの保持を確認した。初期の試験失敗で残った追加の一時領域6件も削除・修復していない。これらはソース27件に含めず、将来も一括ステージングしない。
