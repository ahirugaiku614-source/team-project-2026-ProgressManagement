# PR #20：code-server本文取得・Vue連携の実現性と認証情報保護

## 結論と今回の境界

PR #19がmainへマージ済みと確認した。最新mainは
`08e992478b550d96dd2351c9c81e640a1eaf0111`、開始時はclean。
`feature/code-server-bridge-feasibility`で作業した。

**未保存本文取得はVS Code API上の設計が可能で、注入したdocumentを使うモック検証に成功。**
ただし実code-serverの拡張host、Webview、親Vueを結ぶ安全な経路は未実証である。
実通信の実装・拡張の配備・UI接続は進めない。Originを緩めてつなぐことはしない。
今回追加したのは独立したモック専用ライブラリとテストで、拡張manifestやVSIXも作成していない。

code-serverは言語共通の編集・保存担当。Python/C/C++/Java/JavaScript/TypeScript/C#/Go/Rustを
取得候補として扱えるが、実行対応は別の許可リストである。既存POST /executeはpythonのみ、
RUN_PYTHON・DisconnectedWorker/Engine・既定503を維持。RUN_CODEも多言語実行も追加しない。

## 認証情報の保護

`frontend/src`のconsole、token/password、独自localStorage/sessionStorage利用を調査した。

|対象|変更|
|---|---|
|Login.vue|IDトークン本体と見出しのconsole出力を除去。getIdTokenの完了待ち、新規登録・ログイン、成功時の画面遷移は維持|
|Login.vueのcatch|生のFirebase例外のconsole出力を除去。既知codeに対応する日本語固定表示を維持し、null/未知エラーも固定表示へ変換|
|Home.vueのログアウトcatch|生の認証例外をconsoleへ出さず、既存の固定alertと処理終了を維持|
|Editor.vue|従来の要求時Bearer取得を確認。新たなtoken保存・ログ出力・実行要求を追加しない|

自作フロントエンド内にconsole呼出しと独自local/sessionStorageアクセスが残っていないことを
静的検査した。これはSDK内部、依存ライブラリ、ブラウザー拡張、インフラのログまで監査済み
という意味ではない。Firebase SDKの認証状態の永続化設定は変更していない。
トークンのURL渡し、code-server設定、共有workspaceへの書込みは追加していない。

認証テストは既存の@vue/compiler-sfcでLogin.vueを解析し、importをref/Firebase/routerの
モックに置き換えて既存のhandleSubmitを呼ぶ。vmで扱うのは信頼済みのこのUI定義だけで、
学生コード・エディター本文をvm/exec/evalへ渡すものではない。実Firebaseへ接続しない。
成功と認証/token取得失敗時にconsole出力なし、既存認証呼出し・遷移・固定エラーを確認した。
Homeのログ出力除去は静的検査で確認し、実ブラウザーのログアウト試験はNOT_RUN。

## 現在のcode-server構成と未確認事項

調査対象は`code-server/Dockerfile`、`docker-compose.yml`、既存gateway設計、
`Editor.vue`/`CodeSharing.vue`、前段の`code-server-execution-integration.md`。

|事項|今回確認できたこと|実機結果|
|---|---|---|
|code-server版|Dockerfileは`codercom/code-server:latest`。版・digestが固定されておらず、実際の稼働版/同梱VS Code版はソースから確定できない|NOT_RUN／版未確認|
|Dockerfile|Python/GCC/Node/Git等を含み、最後はUSER coder。変更・buildなし|実image/資源/隔離はNOT_RUN|
|Compose|127.0.0.1:8080公開、共通PASSWORD、code_server_workspaceを一つmountするローカル用構成|User×Project認可・分離を実証した環境ではない|
|拡張host|公式のweb extensionはWebWorker内で動作し、Nodeや親windowのDOMを前提にできない。server側拡張hostとの違いもある|このcode-server版での拡張実行位置はNOT_RUN|
|Webview/iframe|拡張とそのWebviewのメッセージAPIは存在するが、外側Vueとの通信とは別。実Origin/CSP/frame-ancestors/X-Frame-Optionsは未取得|実ブラウザー制約はNOT_RUN|

既存Dockerやcode-serverへ接続/execして版を読む処理も今回は行っていない。
将来の個人環境は独立HTTPSゲートウェイと認証済みUser×Project、世代、本人workspaceだけの
隔離を必要とする。ローカル共通パスワード環境を個人環境の代用にしない。
Firebase・Team/Project認可やエディター無効ゲートを迂回しない。

## 未保存本文の取得モック

`labs/code-server-bridge/capture.mjs`はVS CodeのactiveTextEditor.documentに相当する
注入オブジェクトの`getText()`を一度呼ぶ。明示操作、承認対象documentの同一参照、
file scheme、空authority、指定workspace配下の字句パスを先に検査する。
他root、..、外部URI、閉じたdocument、未知言語ではgetTextへ進まない。

本人workspaceと承認documentを決める実アダプターは未実装である。このモックはsymlinkの
実体や所有者を検査できず、字句検査を実ファイルの認可にしてはいけない。将来の専用環境で
canonical path・通常ファイル・本人所有を信頼済みの層が確認できなければ拒否する。
URIをAPIへ渡して任意のファイルを読ませる設計ではない。

本文は未保存変更を含む全文で、save/fs/terminalを呼ばない。取得前後の版・言語・dirty状態・
対象documentが変われば拒否する。返値はlanguage/code/documentVersion/isDirtyだけで、
パスや認証情報を含めない。インデント/CRLF/文字列を改変しない。
UTF-8 65,536bytesちょうどを許可し、超過・空白だけ・NUL・不正surrogateは拒否する。

言語識別子はpython/c/cpp/java/javascript/typescript/csharp/go/rustを取得候補として固定。
未知言語・aliasを自動変換しない。これらを現在のPython APIへ全て送れるという意味ではない。
PR #19の固定profile/隔離内コンパイル/全言語4枠の将来設計を維持する。

## Vueとの通信手段の比較

|手段|利用できる範囲|必要な検証・判断|
|---|---|---|
|window.postMessage|互いのWindow参照があるブラウザーcontext間|完全一致targetOriginと受信origin/source、payload検査が必要。WebWorker拡張から親VueへのWindow参照が自動で得られるわけではない。今回は実装しない|
|MessageChannel|初期通信でportを安全に結び付けられるcontext間|初回の信頼済みwindowとの結合を省略できない。port自体を本人証明にせず、世代・失効・一回要求を照合する。未配線|
|Webview API|VS Code拡張と、その拡張が作成したWebview間|webview.postMessage/onDidReceiveMessage等は外側VueへのAPIではない。Webviewのopaque/動的Originやframe制約を未確認のまま親へ転送しない|

**実際のVue連携は現時点では成立確認できていないため、接続実装を見送る。**
DOMスクレイピング、ワイルドカードOrigin、Webview sandbox/CSPの解除、tokenを拡張へ渡す
A方式への迂回は採用しない。実ドメインへの通信や許可設定は一切追加していない。

代替案は、学生が明示的に本文をコピーし、将来のVue側の入力欄へ手動で貼り付けて確認する
B方式の暫定フロー。自動clipboard読取・workspace mount・保存ファイル読取は不要だが、
出自やProjectを自動証明できない制限がある。これも今回UIへ実装せず、別PRの選択肢とする。

## 通信受理規則だけの独立モック

`bridge.mjs`は実イベントを購読しない。postMessage/MessageChannel/fetch/WebSocketを呼ばず、
Vueや拡張からもimportされない。既定は無効。labMode=trueはモック操作だけを許可し、
connectionEnabledは常にfalse。接続・保存・実行メソッドは存在しない。
Originは予約済み.testのHTTPSに限り、実ホスト/資格情報付きURL/opaque Origin/'*'を拒否する。

注入したsourceオブジェクトとOrigin、認証済みの想定Binding、generationを固定し、明示操作で
一つだけ保留要求を生成する。nonceは既定ではcrypto.randomUUID。5秒未満の期限を単調時計で
検査し、一回消費する。未知field・余分なtoken・他Team/Project/User・別タブ・旧世代・
期限切れ・時計逆行・再送は拒否する。認証そのものは行わないので、自己申告Bindingを
実システムの認可に使ってはいけない。

同じnonceへの不正返信も再試行せず消費する。nonce衝突は拒否し、履歴は64件上限で停止。
期限切れ要求も自動で接続を復活させず、invalidateして新しい接続文脈を明示的に作る必要がある。
ログアウト/Project切替/frame遷移はinvalidateを呼ぶという契約をモックで検証する。
実UIへ自動接続していないため、現実のログアウト時に呼ばれるという保証はまだない。
全拒否例外は固定文面で、本文/token/生例外を含めない。

## テストと実行環境（2026-11-20）

Windows、Node v24.16.0と既存Python環境を使用。既にあるfrontend/node_modulesのVue compilerを
利用し、パッケージは追加・更新していない。依存がなければインストールせず停止する。

```powershell
# リポジトリ直下。追加したモック検証と認証ログ検証だけ。
node --test labs/code-server-bridge/bridge.test.mjs labs/code-server-bridge/auth-logging.test.mjs
```

- Node：18 PASS、0 FAIL、0 SKIP。取得・通信のモック14件、認証ログの検査4件。
- 既存Python回帰：561件中397 PASS、0 FAIL、164 SKIP（59.457秒）。
  PROJECT_TEST_DATABASE_URLとEDITOR_LAB_IMAGE_IDを空にし、一時SQLite・合成workerで確認。
  既定/executeの503、認証・所属確認、既存実行/エディター/Gitの安全な回帰を含む。
- Python回帰はDockerを呼ぶtest_compose_config/test_editor_container_labを収集対象外とした。
  これら2モジュールはSKIP件数に含めない。SKIPは主にWindows非対応Linux Git/FS、
  未指定の隔離PostgreSQL、Linux専用worker3件。
- 合計：579件中415 PASS、0 FAIL、164 SKIP。NodeとPythonの独立した集計だけを合算。

|実機項目|今回の結果|
|---|---|
|実code-server版・拡張host・未保存documentのgetText|NOT_RUN|
|実ブラウザーでのOrigin/source/MessageChannel/Webview/iframe/CSP/ログアウト|NOT_RUN|
|実Firebaseログイン/ログアウトと依存SDK内部のログ|NOT_RUN|
|Linux fcntl・実Docker/gVisor/rootless・資源制限・学生コード実行|NOT_RUN|

モックの18 PASSを実code-server・実ブラウザーの成功と記録しない。実行機能は一切有効化して
いない。既存Starlette TestClientの非推奨警告は残る。frontendの実ビルド・E2Eも今回はNOT_RUN。

## 残る課題と次PR

次は承認済みの専用検証環境で、code-server/VS Code版とdigest、拡張host、CSP/frame制約、
通常ファイル/本人workspaceの実体確認方法を読み取り専用で確定する段階を推奨する。
そのうえで、本文だけを取得する最小拡張とテスト用Vueの間に安全な経路を作れるかを再評価する。
実行APIやFirebase tokenはまだ接続しない。接続不能なら手動貼付案を比較して設計を見直す。

tokenはVue側の既存認証処理に留め、code-serverにDocker権限やsocketを与えない。
workerへworkspaceをmountしない。既存Docker/Compose/volume、PostgreSQL/DDL、共有Git、
workspace、KVM、Firebase設定と認可、DisconnectedWorker/Engine、実行ゲートは未変更。

## 公式資料（確認：2026-11-20）

- [VS Code TextDocument/activeTextEditor API](https://code.visualstudio.com/api/references/vscode-api)
- [Web extensionとWebWorkerの制約](https://code.visualstudio.com/api/extension-guides/web-extensions)
- [Webview APIの通信範囲](https://code.visualstudio.com/api/extension-guides/webview)
- [code-server FAQ](https://coder.com/docs/code-server/FAQ)
- [Window.postMessageの安全上の条件](https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage)
- [MessageChannelの初期port転送](https://developer.mozilla.org/en-US/docs/Web/API/MessageChannel)

公式APIの存在と、対象環境で安全な経路が実証できることは別である。採否・停止・代替案は
本設計での判断であり、公式資料がこの構成を検証済みとしているわけではない。
