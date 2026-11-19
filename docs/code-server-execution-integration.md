# PR #19：code-serverと実行APIの連携設計・多言語への拡張

## 今回の範囲

PR #18マージ後のmain `e3144c288ba2d932902625b32fcbbafcd02a14c0`を確認し、
未コミット・未追跡変更がない状態から`feature/code-server-execution-integration`を作成。
設計書と既存契約のモック回帰テストだけを追加する。新しい公開API、汎用実行契約、
拡張機能、Vueボタン、HTTP送信、実行アダプターは実装しない。

途中で追加された多言語要件も本書に含める。code-serverは言語に依存しない編集・保存の
環境であり、実行可否を決める機能ではない。候補はPython、C、C++、Java、JavaScript、
TypeScript、C#、Go、Rust。**現在の対応言語はPythonのみで、実workerは未接続503**。
エディターで言語を選べることと、実行環境が対応済みであることを区別する。

## 調査した既存実装

|ファイル|確認内容・再利用方針|
|---|---|
|`frontend/src/components/Editor.vue`|VueMonacoEditorのバッファと`/api/files`によるDB保存。`auth.currentUser.getIdToken()`とBearer送信の既存パターンがある。code-server画面ではなく、Project付き個人workspaceと同一であると推定しない|
|`frontend/src/components/CodeSharing.vue`|画面表示確認用。プロジェクト選択・実行UI・code-server連携は未実装|
|`frontend/src/router.js`|Firebaseのログイン状態で画面遷移を制御。サーバーの現在所属確認の代替にはならない|
|`frontend/src/firebase.js`、`components/Login.vue`|Firebase SDKを利用。Login.vueにIDトークンのconsole出力が残っている。次PRでの接続前に除去とログ監査が必要。今回変更せず、安全な状態と報告しない|
|`docker-compose.yml`、`code-server/Dockerfile`|共有パスワードのローカル検証用code-server、専用volume、言語ツールの構成。User×Projectを隔離した本番エディターとして転用しない。変更・起動・buildなし|
|`backend/app/editor_api.py`、`editor_contracts.py`|既存所属確認を再利用するEditorBindingと、起動/停止/接続の503ゲート、WS全拒否。解除しない|
|`backend/editor_gateway/gateway.py`、`docs/editor-gateway-reference.md`|独立HTTPS認可・所有者/世代・短期セッションの参照設計。実HTTP/WSプロキシやcode-server接続は未実装。モックのsafe_readyを本番証跡にしない|
|`backend/app/main.py`|`create_execution_router`を登録。`get_project_team_member`と`get_accessible_project`で現在所属・Project/Teamを照合。CORSはlocalhostの開発用設定。変更しない|
|`backend/app/editor_access.py`|Firebaseトークンをcheck_revoked=Trueで確認。失効/障害時も拒否し、raw認証エラーを返さない|
|`backend/app/schemas.py`|ExecutionCreate、ExecutionResponse、64KiBのUTF-8 byte上限、追加field拒否。既存バリデーションを使用|
|`backend/app/execution_api.py`、`execution_client.py`|POST /execute、認可済みEditorBinding、RUN_PYTHON、UUID、固定ポリシー、応答照合、既定DisconnectedWorker。認証・結果契約を変更しない|
|`backend/execution_worker/{auth,engine,registry,service}.py`|署名付き短期要求、固定Python image/spec、単一daemon/profile束縛、共有SQLite4枠、完全ID、UNKNOWN保持。多言語対応を実装済みと扱わない|
|`docs/python-worker-privilege-boundary.md`|rootless専用daemon＋限定ブローカーの検証候補、code-serverへのDocker権限禁止、直接workspace mount禁止を維持|

実Firebaseの認証情報ファイル、秘密鍵、.envの値は調査に不要なので読み取らない。

## 連携方式の比較と選定

|観点|A：拡張がAPIへ直接要求|B：Vueだけから要求|C：code-serverの本文をVueへ渡し、Vueが要求|
|---|---|---|---|
|難易度|高。拡張hostの種類、Firebase認証、API接続を新規実装|既存Monacoなら低。ただしcode-serverの現在本文は取得できない|高。安全なブラウザブリッジが必要だが認証・API通信をVueへ集約できる|
|未保存コード|documentの現在本文を取得可能な設計|code-serverの未保存バッファへ直接アクセス不可。保存ファイル読取APIを足す案は別の権限境界が増える|明示操作時のdocument全体を一回スナップショットとして渡す|
|Firebase|拡張へtokenを渡すと、拡張hostや保存設定から漏れる危険。独自認証も重複|Vueの既存SDKを利用できる|tokenをVueに留め、code-serverへ渡さない|
|Origin/CSRF|code-server OriginやCORS対応が増える。サーバー側拡張ならブラウザ制限も境界にならない|Vue/APIの既存経路を設計できる|正確なOrigin/sourceと一回の要求照合が必要。bridgeは認証の代替にしない|
|User×Project|拡張の自己申告を信用できず、別途現在所属照合が必要|Vueの選択ProjectとAPIの現在所属照合|認証済みエディターのBinding/世代とVue選択を照合し、APIでも再認可|
|既存実装との整合|認証・エラー処理が分散しやすい|既存Monacoの本文を使うなら容易だが、今回のcode-server要件を単独では満たさない|EditorBinding、Vueの認証、既存POST /executeを分業して再利用できる|

**Cを将来実装の推奨案とする**。code-server側は本文・言語・ローカル版数の取得だけを担当し、
Vue側が利用者の明示操作を受けてAPIへ要求する。Aの直接通信は採用せず、BはCのAPI呼出し側
として利用する。認証を拡張へ重複実装しないことが主な理由。推奨案は本設計の判断である。

ただし、VS Code拡張から親Vueへ安全に届く経路はまだない。web extensionはNode.js APIを
使えず、拡張APIが利用できても親windowのDOMを自由に操作できるとは限らない。
code-serverの版・拡張host・webviewの実Origin・CSP・frame制約を調査して、限定ブリッジの
実現可能性を次PRで確認する。DOMスクレイピング、Origin='*'、制約解除、サーバー側の
任意ファイル読取で代替しない。安全な経路を作れなければ接続を停止し再設計する。
[VS Code公式web extension資料](https://code.visualstudio.com/api/extension-guides/web-extensions)

## 本人のバッファを対象にする規則

- 初期の対象は、認証済み本人のUser×Project環境で開いているアクティブdocumentの
  **未保存変更を含む全文**。選択範囲だけ・ディスク保存済み版・workspace全体ではない。
- 明示操作時に本文、言語、document版を一度取得し、後からバッファが変わっても要求本文を
  差し替えない。自動保存はしない。結果には「取得時点の未保存内容」という表示を検討する。
- 実行候補として送れるのは、管理された本人workspace内の通常テキストdocument。
  multi-root/外部URI/シンボリックリンク経由/untitled文書は初期対応外とし、検証不能なら拒否。
  URIはエディター側の候補選別だけに使用し、サーバーへ読み取り対象として渡さない。
- ファイル名・host path・volume・URIからFastAPIやworkerがファイルを開くAPIは作らない。
  labelや拡張子だけで言語・所有者を決めない。workerへ渡すのは検証済みコード本文だけ。
- 本文上限は全言語共通で64KiB（65,536 UTF-8 bytes）。インデント・改行は保持し、
  空白だけ/NUL/不正Unicodeは既存検査で拒否。文字数ではなくbyte数で数える。
- 現APIは任意の本文を受け取るため「学生が自分で書いた文章か」は証明できない。
  この要件で保証するのは環境の所有・現在のアクセス権・他人の保存領域を取得しないこと。
  クライアントが壊れている場合の本文の出自まで証明済みとはしない。

## C方式のブラウザ側契約（設計のみ）

1. Vueは将来の認証ゲートウェイから本人のEditorBindingと有効な環境generationを確認する。
   URLのProjectやcode-serverの自己申告だけでは環境を確定しない。現在はゲートが閉じており進めない。
2. 利用者の操作により、Vueは推測困難な要求nonce、有限の短期期限、接続世代をメモリに保持。
   固定のエディターwindow/portへ本文取得を依頼する。nonceは認証tokenではない。
3. 返信では完全一致のOriginと実際のsource window、期待する接続・要求nonce・generation・
   Project・言語・document版・schema・UTF-8サイズを照合する。送信元のuser_idは信用しない。
   返信は一回消費し、不明field、重複・期限切れ・別タブ・別Project・旧世代を拒否する。
4. Project切替、ログアウト、frame遷移、エディター再作成で保留要求と本文を破棄する。
   送信直前にもログイン/選択Projectを再検査する。本文取得だけで自動実行しない。
5. Firebase IDトークンはVueで要求時に取得し、信頼する固定API宛てのAuthorizationヘッダーだけに
   使用する。bridge本文、URL/query、code-server設定/作業ファイル、ログ、共有volumeへ渡さない。
   Firebase SDK既存の永続化方式の点検は別途必要で、現在の認証情報が全てメモリ限定とは主張しない。
6. 将来の要求はJSON＋Bearerを使い、APIへのfetchはcredentials:'omit'を前提にする。
   CookieをPOST /executeの認証代替にしない。固定HTTPS宛先・redirect拒否・限定OriginのCORSを
   別PRで検討する。CORSは認証でも本人証明でもなく、非ブラウザのアクセス制御はAPIの認可で行う。

postMessageを使う場合はtargetOriginを正確に指定し、受信時にorigin/sourceと形式を検査する。
opaque Originのwebview等で正確な照合ができなければ拒否し、'*'に緩めない。
MessageChannelを使う場合も最初のwindowとの安全な結合・port破棄・一回消費が必要。
この手順のnonce/generation/版数はブラウザ連携用で、既存POST /executeへ追加fieldとして送らない。
[MDN postMessageの安全要件](https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage)

## 現APIを維持する経路

現在のJSON本文は`team_id`、`project_id`、`language: "python"`、`code`のみ。
user_id、path、image、command、compiler_options、operation、version等を追加してはいけない。
APIがFirebase UIDからUserを解決し、現在のTeamMemberとProject/Team対応を確認して
EditorBindingを確定、UUID付きExecutionCommand（operation=RUN_PYTHON）へ変換する。
ブラウザの認可結果だけでworkerへ直行しない。既存の固定ExecutionPolicyも変更しない。

|結果|既存契約・将来のUI方針|
|---|---|
|200|ExecutionResponseのstatus/stdout/stderr/exit_code/execution_time_ms。学生プログラムのerror/timeoutも結果として200。UIはテキスト表示しHTML・ANSI・リンクを実行しない|
|401/403|未認証・失効・所属なし等を既存依存関数で拒否。勝手なtoken再利用/別Projectへのretryをしない|
|404|Project不明/Team不一致等。存在確認のために別IDを探索しない|
|422|型・上限・未対応言語・追加fieldの不正。コードやtokenを含むraw検証エラーを表示しない|
|429|混雑、Retry-After付き。無限自動再送をせず、新しいnonceや枠で迂回しない|
|503|worker未接続/不正応答/基盤障害。現在の既定。UI追加によって正常実行へ変換しない|

ExecutionResponseは実際には6フィールドではなく、上記**5フィールド**である。
既存資料の「6フィールド」という表記よりschemas.pyの実装を正とする。今回変更しない。
stdout/stderrは各64KiB。コード/token/内部パスをエラーへ反射させず、学生出力も機密を含む
可能性があるためconsoleや監視へ自動記録しない。token・本文付き例外をUIへ生表示しない。

## 多言語の許可リストと固定プロファイル（将来案）

|候補表示名|将来の正規language識別子|現在のPOST /execute|
|---|---|---|
|Python|python|形式上許可、実workerは未接続503|
|C / C++|c / cpp|422で拒否|
|Java|java|422で拒否|
|JavaScript / TypeScript|javascript / typescript|422で拒否|
|C#|csharp|422で拒否|
|Go / Rust|go / rust|422で拒否|

許可リストはサーバー管理のバージョン付きカタログとする。候補一覧への記載は有効化ではない。
将来も未実装・検証不十分なprofileを実行可能一覧へ出さず、未知の値・alias・大文字違いから
自動推測しない。Vue表示用の言語一覧も認可済みの稼働対応情報を使い、エディターのsyntax設定を
実行許可にしない。候補すべてが同じメモリで動く、インストール済みだとは主張しない。

workerはlanguageから固定profile（immutable image digest、固定entrypoint、toolchain版、
固定ソース名/コンパイラ引数、資源上限、段階別期限）をサーバー側で選ぶ。
学生はimage、実行コマンド、コンパイラオプション、package manifest、環境変数、mount、
network、profile_id、timeoutを指定できない。自動pull/buildや依存downloadはしない。
初期の汎用案も単一ソースのみで、複数ファイル・stdin・外部パッケージは未対応として拒否する。

### 資源・期限の検討値（全項目未実測・NOT_RUN）

以下は次の専用環境で計測するための候補値であり、推奨スペックの動作確認結果ではない。
現在のPython 0.5CPU/128MiB/5秒を変更する設定表ではない。

|言語|コンパイル等のメモリ上限候補|コンパイル等の期限候補|実行メモリ候補|実行期限候補|
|---|---|---|---|---|
|Python|なし|なし|128MiB（現契約）|5秒（現契約）|
|C|256MiB|10秒|128MiB|5秒|
|C++|512MiB|20秒|128MiB|5秒|
|Java|1GiB|20秒|512MiB|5秒|
|JavaScript|なし|なし|256MiB|5秒|
|TypeScript|512MiB|20秒|256MiB|5秒|
|C#|1GiB|30秒|512MiB|5秒|
|Go|512MiB|30秒|128MiB|5秒|
|Rust|1GiB|30秒|128MiB|5秒|

JVM/.NET/Nodeの起動、compiler/linker、CPU0.5による遅延、ARM64/x86_64を実測してから
採否を決める。compilerの内部heap/thread制限もcgroup上限と整合させる。未成立なら言語を
無効のままにし、OOM回避を理由に無制限へ広げない。stdout/stderrは言語によらず各64KiBを
初期候補として維持し、compile診断＋実行出力の**合計**にも適用する。

4件上限は全言語・全phaseで共有し、compile開始前から停止/削除/DB完了確認まで1枠を保持。
phase切替やtimeoutで先に枠を返さない。別言語へretryして枠を迂回しない。
将来は件数に加えprofileの最大メモリ等を原子的に予約する必要がある。4件×最大1GiBなら
学生分だけで4GiBに達するため、host/daemon/runscの余裕を加えて容量不足で新規要求を拒否する。
これは容量試算で、無料クラウドでの成立や実使用量の保証ではない。

### コンパイルも任意コード実行として隔離する

compiler/annotation processor/macro/build処理自体を攻撃対象とし、コンパイルも実行も
runsc内で行う。host上のsubprocessでcompileしない。network none、非root、cap全削除、
no-new-privileges、read-only root、pids/swap/一時容量制限を各段階で強制する。
学生のDocker socket・host/他人workspace・秘密をmountしない。

将来の固定launcherが隔離内でcompile→実行を管理し、APIはコンパイラコマンドを渡さない。
build.rs、ユーザー指定build設定、外部module解決等を初期対応から除外する。子processも含む
停止・出力回収・compile timeout/実行timeout/OOMの結果を設計し、任意exec APIを追加しない。
既存Pythonの/work tmpfsはnoexecのため、C等の生成バイナリをそのまま実行できると仮定しない。
将来の別profileで容量制限付き専用artifact領域の実行可否と残存リスクをレビューし、
既存specのnoexecを外す変更で対応しない。phase間の成果物も同一jobの隔離領域に限定する。

## RUN_PYTHONからRUN_CODEへの互換移行案

1. 現在のPOST /execute、ExecutionCreate/Command/Response、RUN_PYTHON、署名v1を固定する。
   languageのLiteralを一括拡張してPython entrypointへC等を流す変更は禁止。
2. 将来の別PRで明示的なv2入口（候補：POST /api/v2/execute）と内部RUN_CODE v2を追加検討。
   外部本文はteam_id/project_id/language/codeに限定し、operationはAPIが決める。
   現在のExecutionResponseを再利用できるか検討し、compile失敗は固定診断＋error、timeoutは
   exit_code=null等の意味を明文化する。未対応言語をPythonへfallbackしない。
3. 新旧とも同じ認証・現在所属解決を再利用し、EditorBindingと実行UUIDへ要求・応答を束縛。
   v2は署名domainも分け、languageと本文全体を認証する。v1をv2として解釈しない。
4. 既存Registry.bindは単一image/specのprofile hashを束縛している。多言語化には、
   daemon＋承認済みcatalogのhashとjobごとのprofile/digestを保持する台帳版の設計が必要。
   単純にbindを省略したり、言語ごとに別DBを作って4枠を増やしたりしない。
5. 同じdaemonを操作する新旧入口を、両契約に対応した単一の予約基盤へ集約する。
   実行ID/nonce/hash/revision、未完了/UNKNOWNの証跡を保持し、同一jobのsnapshotを再解釈しない。
   旧writerとの混在禁止、排他、既存記録の保存・再実行安全な明示移行を別PRでテストする。
6. 全体4件、phase別/全体期限、制御I/O期限、独立監視、所有確認付き復旧を実証してから
   言語を一つずつ有効化する。現在は実adapterも独立watchdogもなく、5秒停止保証はない。

このPRにはv2ルーター・モデル・migration・catalog実装を追加しない。多言語対応を理由に
既存SQLiteやPostgreSQLへDDL、既存台帳/環境の初期化や移行を行わない。

## 検証コードと合格条件

新しい認証やバリデーションを重複実装せず、`test_execution_integration_contract.py`で
既存APIの入力検査・認可を一時DB、合成Firebase、結果固定workerで確認する。
TestClientはin-process ASGI試験で、実HTTPサーバー/外部APIへ送信しない。コードは評価しない。

- 未保存本文相当のインデント/改行と認証済みBindingが保持される。
- 将来候補言語、RUN_CODE、任意image/command/compiler_options、path/token/bridge fieldを拒否。
- 未認証・他Project・バッファ取得後の所属削除ではworkerへ到達しない。
- 不正入力のコード・認証情報はエラー応答へ露出せず、UTF-8の64KiB上限を維持。
- 実worker未接続は既定503。既存worker/API/clientと他の回帰テストも実施する。

ブラウザのOrigin/source、bridge一回消費、XSS/CSRF、code-server拡張API、実エディター本文、
多言語コンパイル、実Docker/runsc/rootless、実資源制限、実停止はNOT_RUN。
APIのモック成功をブラウザ連携や多言語隔離の合格と解釈しない。

## 次のPRで行うこと・停止条件

次PRはまずcode-serverの限定本文取得経路の実現性と安全性を、認証情報を渡さずに検証する。
Login.vueのtoken出力除去、ログ監査、適切なOrigin/CSP/frame制限、Project/世代切替、
未保存snapshotの明示、secret非表示の結果UIを設計・試験する。その後にのみ、Vue→既存
POST /executeの明示操作をモック/既定503へ接続する案をレビューする。Docker実接続とは別PR。

複数言語のv2・catalog・台帳移行・compile launcherはその後の段階で、Python契約を維持して追加する。
安全なbridgeや本人環境を証明できない、token漏えいが残る、任意path/commandが必要になる、
既存無効ゲートを回避する場合は接続を停止して設計を見直す。

## テスト結果と既存環境の保護

Windowsの既存開発用Pythonで、実DB/実エディター向けの環境変数を空にして実行した。

```powershell
# backendで実行。外部DBやDockerを指定しない。
$env:PROJECT_TEST_DATABASE_URL = ''
$env:EDITOR_LAB_IMAGE_ID = ''
..\.venv\Scripts\python.exe -B -W ignore::DeprecationWarning -m unittest discover -s tests -p 'test_execution*.py' -q
```

- 実行系：**119件中116 PASS、0 FAIL、3 SKIP**（26.866秒）。新規9件はすべてPASS。
- 既存回帰全体：**561件中397 PASS、0 FAIL、164 SKIP**（65.122秒）。
  全体の集計には上記119件を含み、合算しない。
- 全体回帰は`test_*.py`からDockerを呼ぶ`test_compose_config`と
  `test_editor_container_lab`を収集対象外にした。この2モジュールはSKIP件数に含まない。
- SKIPは主にWindows非対応のLinux Git/FSと隔離PostgreSQL未指定、およびLinux専用worker3件。
  Linuxで実行した結果ではない。実Docker/gVisor/rootless・多言語実行・ブラウザbridge・
  実mTLS・実5秒停止はNOT_RUN。
- 既存Starlette TestClientの非推奨警告あり。パッケージ追加・更新なし。

変更は本書と新規モックテストの2ファイルのみ。既存のコード/認証/Compose/volume/DB/
workspace/Git、KVM、エディター実行ゲート、DisconnectedWorker/Engineを変更していない。
実DockerやgVisorへ接続していない。
PR #17の利用者Ubuntu結果、PR #18のWindows結果は元資料の履歴とし、PR #19の実績へ合算しない。
