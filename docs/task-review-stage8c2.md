# Stage 8-C2：初心者向けコード共有・変更レビュー画面

## 実装範囲

既存CodeSharing.vueの仮表示を、Firebase認証と実APIを使う画面へ置き換えた。
画面文言・プログラム内コメントは日本語を基本とし、以後もこの方針を維持する。
業務操作をworkflow.mjs、通信をapi.mjs、表示をCodeSharingPanel.vueへ分離した。
本工程でバックエンド、DB、Linux、実provider、既存検証拡張は変更していない。

変更ファイル：

- frontend/src/components/CodeSharing.vue：既存認証・Routerとの接続。
- frontend/src/components/CodeSharingPanel.vue：自分の作業、変更一覧、確認、レビュー詳細。
- frontend/src/code-sharing/api.mjs：Bearer認証、固定エラー、通信期限・応答量の上限。
- frontend/src/code-sharing/workflow.mjs：状態遷移、確認ID、操作ラッチ、旧応答の破棄。
- frontend/src/components/Home.vue：コード共有と既存エディタへの導線。
- frontend/src/components/Editor.vue：未保存警告を維持する通常遷移リンク、保存と提出の区別。
- frontend/src/components/schedule.vue：担当タスクとレビュー画面への導線。
- frontend/tests/workflow.test.mjs、frontend/tests/render.test.mjs：合成状態・API・実Vue描画の検証。
- frontend/package.json：Node標準テストコマンドを追加。依存ライブラリは追加しない。
- frontend/.gitignore：合成静的表示・検証成果物のartifactsを除外。

## 画面遷移と既存機能

ホーム／進捗管理／既存エディタ → `/CodeSharing` → Team → Project → 自分の担当Task。
現在の選択は `?team=<ID>&project=<ID>&task=<ID>` で共有できる。確認IDや本文・許可値はURLへ入れない。
既存Routerの認証ガードとFirebaseのgetIdTokenを利用し、別のログイン機構は作らない。
Editorへの移動後もコンテキストIDを戻せる。Editorの保存は従来どおりで変更提出を呼ばない。
Editorからの通常遷移では既存beforeunload警告が動作する形を維持し、強制保存はしない。
旧Homeのサンプル情報やscheduleの仮画面を、実情報であるかのようにCodeSharingへ流用しない。
CodeSharing内の担当タスクはTeam Task APIと現在ユーザーのassignee_idから読み取る。
現在のAPIにはTask→Project対応の一覧がないため、選択後にサーバーが対応を検証し、不一致はエラーにする。

## 利用するAPI

| 目的 | 実際の入口 |
|---|---|
| ユーザー・所属 | GET /api/me、/api/teams、/api/teams/{team}/members |
| Project・担当タスク | GET /api/teams/{team}/projects、/api/teams/{team}/tasks |
| 作業環境の確認 | GET /api/teams/{team}/projects/{project}/editor |
| 基準・収集・確認・提出・結果 | Stage 8-C1のTask配下work-bases API |
| 収集の短命許可 | Task配下POST submission-permits |
| 一覧・詳細・差分 | Project配下GET change-requests、/{id}、/{id}/diff |
| レビュー・統合 | 既存POST /{id}/approve、request-changes、integrate |
| 統合結果 | 既存GET /{id}/integration |

一覧は10件ずつ取得する。統合状態は各依頼の既存integration APIから読み、未取得を未統合と推測しない。
フィルターは現在のページに適用することを画面に明記する。全件統計であるかのような数値は作らない。
Taskの進捗は表示のみで、提出・承認による自動DONE更新はない。

## 状態と表示

作業準備中／作業中／作業基準の失効、収集中／収集済み、変更確認済み／変更なし、
レビュー依頼作成中／レビュー待ち／修正が必要／承認済み／統合済み／確認が必要を分離する。
SUBMITTEDという収集状態だけではレビュー依頼作成成功を表示しない。
READYと有効なChangeRequest ID、またはNO_CHANGEという確定応答を確認してから結果を表示する。
作業開始時の基準OID、一覧の関連Task・ファイル数、詳細の修正前依頼との対応はAPI未提供と表示する。
差分を取得できた詳細では、その実際のファイル数・追加・修正・削除を表示する。
基準内の除外パスだけを表示し、収集時点の全除外一覧を取得できたとは扱わない。

## 変更確認・明示提出

明示収集 → 候補からの削除確認 → タイトル・説明入力 → preview → 確認ID付き明示提出 → 結果照会。
保存・ページ表示・再読込みではPOSTによる変更提出をしない。previewでChangeRequestを作らない。
タイトル等が変わると確認を解除する。期限も検査し、バックエンドのPREVIEW_CHANGED/EXPIREDでも再確認へ戻す。
バックエンドのnaive日時はUTCとして解釈し、表示時だけローカル時刻へ変換する。
削除は候補IDだけから選択でき、初期選択なし・確認ダイアログ必須。
削除証拠の取消し・一覧照会は未提供なので、誤選択や応答不明時は提出を止めて管理者確認とする。
全削除の空収集・未対応空treeは固定エラーから理由を表示し、変更なしへ変換しない。

request IDは明示収集時に一度だけ発行する。操作中は単一ラッチで二重クリックを防ぐ。
sessionStorageにはユーザー/Team/Project/Task別の基準ID・request ID・収集ID・保留操作だけを保存する。
本文・差分・タイトル・確認ID・permit・Firebaseトークンは保存しない。
保持できない、または破損している場合は新規操作を拒否する。
通信不明時には同じ収集IDのGET結果確認を行い、別request IDで自動再提出しない。
収集IDそのものを受け取れなかった場合、request IDから照会するAPIがないため管理者確認が必要になる。
「次の変更を準備」は既存結果がREADY/NO_CHANGEと確定した後の明示操作だけに限定する。
修正後は新しい収集結果を使い、前の依頼IDを既存previous_request_idへ渡す。

## レビュー・権限・統合

現在の所属・ユーザー・Project・提出者・OPEN/PENDING・差分が確認できた場合に、本人以外のレビューを許可する。
統合は既存条件に合わせ、別メンバーの承認済み・OPEN・既知の未反映状態のときだけ要求できる。
統合前には確認ダイアログを表示する。PREPAREDや統合結果未取得なら操作を無効化する。
最終認可・基準一致・排他処理は既存バックエンドに任せ、画面判定を認可の代わりにしない。
mainの現在OIDはAPIから取得できないため、一致を確認したと表示しない。
サーバーの統合拒否は固定文言で説明し、自動マージ・強制統合を行わない。
レビュー・統合POSTの応答が不明な場合は依頼ID単位で保留し、詳細・integration GETで確認する。
未確定を「操作なし」と推測して再POSTしない。既存判定を上書きするAPIは作らない。

## 通信・表示の安全性

Team/Project/Taskの世代と、一覧・詳細・基準読取りの連番で、古い応答を破棄する。
GETは切替時に中断できるが、POSTの中断や画面移動をサーバー側取消しとみなさない。
認証ユーザー変更でも古いコンテキストと表示を破棄し、別ユーザーの操作記録を使わない。
通信は15秒、応答は2MiBまでとし、無制限な差分を保持しない。
差分はテキスト描画のみ。v-html/innerHTMLやコード実行機構は使わない。
40ファイルずつ表示し、各パッチは20,000文字まで、横スクロールと省略表示を用意する。
不正構造や基準・snapshot不一致の差分は描画せず、承認操作も無効化する。
APIの未知の内部例外を表示・ログ出力せず、固定エラーコード・HTTP状態から日本語で説明する。

## 実接続を有効にしない境界・APIの不足

本番コンテナーはsessionSourceを注入しない。実セッション取得・保存状態認証の公開成功契約がないため、
収集・変更確認・変更提出は無効化される。画面に「保存済み」と表示して自己申告で代替しない。
作業環境APIのlaunch_enabledもfalseなので、通常の画面で作業開始は無効になる。
純粋モデルの成功試験だけが明示的なFake API・Fake sessionSourceを注入する。製品画面にテスト切替はない。
APIエラーで架空の成功一覧へ切り替えない。既定provider・実検証器はバックエンド側も拒否のままである。

Stage 8-Dに向けて別途設計が必要なもの：

- 編集セッションの認証済み取得・失効・dirty検査契約と、実workspace初期化証拠。
- TaskとProjectの対応一覧、基準バージョンの表示用メタデータ。
- ChangeRequestのTask・ファイル数・修正前依頼との対応の読取り契約。
- request IDによる結果照会、削除証拠の照会・取消しと、応答不明時の明示復旧。
- 007/008の適用承認、Linux/PostgreSQL合成検証、実provider有効化の別承認。

これらの不足を補うバックエンドAPIは本工程で推測・新設していない。

## 検証結果（2026-11-25）

- npm test：Node標準の状態・API契約試験53件＋実VueのSSR描画6件、合計59 PASS、0 FAIL、0 SKIP。
- npm run build：PASS。約631KBのminify済みチャンクについてViteの500KB警告あり。
- 型検査・lint：既存package.jsonにコマンドがなく未実施。実施済みとは扱わない。
- 新規依存ライブラリなし。既存lockfileに従うnpm ciを使用し、lockfileは変更していない。
- 初回オフライン導入はstate-localのキャッシュ不足で失敗し、通常の既存依存導入で解消した。
- 初回状態試験は41 PASS・1 FAIL。破損記録に対する収集ガードの不足を修正し、追加の境界検査を含む最終59件を再実行してPASSした。
- バックエンドは前回と同じ390件を再実行し、285 PASS、0 FAIL、0 ERROR、105 SKIP。
- SKIPはLinux固有103件と、隔離PostgreSQLを要する2件で前回と同じ。
- git diff --check：PASS。バックエンドコード・マイグレーションは本工程で変更なし。

## 合成画面の確認方法

frontend/artifacts/code-sharing-synthetic.htmlをブラウザーで開くと、合成データによる静的表示を確認できる。
npm testのSSR試験から生成される。実Firebase・実APIへ接続せず、操作は動作しない。
画面内にも合成・静的表示である旨を明記する。コードや認証情報の実データは使っていない。
スクリーンショットと、実ブラウザーでのイベント・レイアウトの目視確認は未実施である。
認証を含む実画面を確認するには、設定済みFirebaseと認可されたFastAPIが必要であり、今回起動していない。

旧worktreeの提出基盤7ファイルは既存バックアップと直接バイト比較で全件一致し、バックアップ10ファイルを保持した。
commit・push・PR作成、実DB、Linux/Docker、PR #21環境、実provider有効化は操作していない。
A2未完了、B1未承認、SSH255分類Hを維持する。
