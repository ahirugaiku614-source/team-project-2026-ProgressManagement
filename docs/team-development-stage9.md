# Stage 9：チーム開発MVP

目的は初心者向けの共同開発支援です。保存、変更確認、レビュー依頼、承認、共有コードへの反映は別操作です。
既存ChangeRequestを再利用し、別の課題提出・承認機能は作りません。

## 実装範囲と利用可能性

| 機能 | 実装 | 今回の検証範囲 |
|---|---|---|
| request IDによる結果照会 | 本人・Team・Project・Task・基準に限定したGET | 合成API |
| 確認ID・削除証拠照会 | 状態・期限・基準を照合。GETから再収集しない | 合成API |
| 基準バージョン・関連タスク表示 | 既存記録から取得 | 合成API・Vue |
| 作業環境準備・新世代取得 | 先行受付、重複拒否、旧環境保持 | 合成Git・DB |
| Linuxファイル初期化 | 固定root・固定メンバー対応・新規領域だけ | 構文・Windows拒否。Linux実証なし |
| 編集セッション | 永続scope・期限・失効・毎回のchallenge | 管理側合成authorityのみ |
| code-server接続 | 短命のHttpOnly/Secure/Strict cookieと読取り認可gate | 合成HTTPのみ |
| 新しい提出拡張 | 明示コマンド・確認・dirty時拒否・管理画面への導線 | Nodeモック |
| 確認前のコード差分 | 上限付きUTF-8テキスト／バイナリ区別、HTML非実行 | 合成API・Vue SSR |
| 承認・統合 | 既存API・認可・Git CAS・履歴を再利用 | 合成2人操作。Linux永続性は未実証 |

実provider、実セッション検証器、workspace検証器の既定拒否は維持しています。
アプリ起動時に009を自動適用する処理、HTTPから模擬providerを選ぶ機能はありません。

## API追加・拡張

共通起点：`/api/teams/{team}/projects/{project}/tasks/{task}/work-bases`

- `GET /{basis}`：従来応答に`base_version / workspace_id / source_generation`を追加。
- `GET /{basis}/requests/{request}`：収集応答を失った場合の読取り照会。未登録は`UNKNOWN`。
- `GET /{basis}/confirmations/{confirmation}`：AVAILABLE / EXPIRED / INACTIVE / CONSUMED。
- `GET /{basis}/deletion-intents?submission_id=...`：本人の当該収集へ結合した削除証拠。
- `GET /environment`：準備設定と接続条件。準備済み・保存済みとは断定しない。
- `POST /environment`：request_id、任意のprevious_basis_idとsession_id。利用者ID・パス・OIDは受け付けない。
- `GET /environment/requests/{request}`：準備結果照会。未登録も未実行の証明ではない。
- `POST /{basis}/editing-sessions`：管理側authorityで検証してから発行。IDだけで認証しない。
- `POST /{basis}/editing-sessions/{session}/revoke`：本人のセッションだけ失効。保存済みコード・基準は削除しない。
- `POST /{basis}/editor-access`：認可済みsessionに対応する固定workspaceへ短命cookieを発行。
- `GET /api/editor-gateway/{workspace}`：cookie・期限・現在権限・管理側sessionを検証する認可gate。状態を更新しない。
- `GET /api/teams/{team}/projects/{project}/change-requests/{id}/task-context`：既存依頼とTask・修正前依頼の対応。

確認APIの既存`changes`形式は維持し、`diff`を追加しました。確認だけではcommitや依頼を作りません。
コード本文を表示できる契約へ拡張したため、旧テストの「確認応答に本文がない」という期待だけを更新しました。
除外ファイル、認可、容量制限、明示提出の条件は維持しています。

## 作業環境と最新版取得

準備受付をDBへ先行記録し、同じ操作は既存結果を返します。別入力でのID転用と、未確定処理中の別ID準備を拒否します。
初期化が失敗・応答不明なら自動再実行しません。現在の基準だけを書き換えて既存workspaceを正当化しません。

最新版取得は現在のsessionの保存状態と、保存済みファイル全体が作業開始時点から変わっていないことを管理側で確認します。
未保存・未提出変更は拒否し、許可時にも新しいworkspaceと基準を作り、旧領域・参照・履歴を残します。
旧環境の終了に失敗してもファイルを削除・上書きせず、準備結果を不明として管理者確認にします。

LinuxWorkspacesは全体treeの検証済みファイルだけを受け取り、root所有者・権限、正規UUID、
FD相対のno-follow、単一リンク、特殊ファイル、所有者、容量、深さ、二回照合を検査します。
同一FSのbind mountは`st_dev`だけでは検出できないため、FDのmount IDも比較します。
管理rootは専用管理ワーカーの固定メンバーUID/GIDで0700、証拠と通常ファイルは0600です。
メンバーへ公開するのはworkspaceディレクトリだけで、グループ書込みは許可しません。
FastAPIからメンバー別管理ワーカーへの実初期化RPC/配置bootstrapは未接続です。

## 編集セッションの信頼境界

Firebaseで認証した本人からscopeを確定し、DBにsessionと期限を保持します。
authorityの応答はsession・基準・workspace・世代・新規challenge・期限・単一編集経路へ結合します。
拡張は本文取得、強制保存、署名鍵保持、保存状態の認可を担当しません。

**実code-serverの独立したauthorityは未接続です。実利用者向けMVPが完成したとは扱いません。**
複数ウィンドウ・プロセス偽装・利用者による拡張変更を、管理側から識別する仕組みと実証が必要です。
短命cookieだけでは既存WebSocketの失効切断を保証しません。gatewayを実接続する前の停止条件です。
同一originのVue/API/TLSを前提にし、cookie値はURLやJSON・DB・ログへ保存しません。

## 合成デモの操作

1. 指定worktreeの`frontend`で`npm run dev -- --host 127.0.0.1`を実行。
2. `http://127.0.0.1:5173/stage9-demo.html`を開く。通常ログイン・実Firebaseは使わない。
3. Aの「作業環境を準備」。上部の合成エディターを変更し「合成ファイルを保存」。
4. 「編集セッションを確認」「保存済みファイルを収集」。タイトル入力→変更確認→明示提出。
5. Bへ切替え、一覧の変更を選び、差分を確認して承認・「チームに反映」。
6. Bの環境を事前準備していた場合は、編集セッション確認→最新版取得で新世代を作る。
7. 「新しい環境の内容を表示」で更新内容を確認する。

上部エディターは実code-serverの代替を明記したメモリ内デモです。実ファイルを読み書きせず、
入力をプログラムとして実行しません。通常画面がAPI接続に失敗してもこのデモへ切り替わりません。
デモAPIは操作説明用です。製品API/RPC/Git/DBの接続は別の合成2人回帰試験で検証します。
静的SSR HTMLは`frontend/artifacts/code-sharing-synthetic.html`で、操作できるデモとは区別します。
`npm run build:demo`でデモだけの配布物を`frontend/artifacts/stage9-demo`へ生成できます。
通常の`npm run build`の配布物には合成デモを含めません。

## 障害・検証・後続条件

レビュー登録のcommit応答喪失でも、確定済みREADY/NO_CHANGEを古い状態へ戻さず証拠を再照合します。
PostgreSQLの認可ロックには5秒のlock_timeoutを追加しましたが、DB/Gitロック順序の全面統一はしていません。
同期収集callbackの強制中断は未解決です。実データ接続・実provider有効化の前提条件として残します。

Stage 10では009の隔離DB移行、UID/GIDとFD/マウントのLinux実証、認証gateway・全編集経路の管理、
WebSocket失効、実セッションの真正性、収集強制期限を検証・解決してから接続を検討してください。
新しい配置ファイルは`labs/team-development`、既存管理側UDS・台帳は`labs/saved-submission`を参照します。

Stage 11へ延期：診断コード整理、旧用語、高度な競合解決・履歴検索、チャンク最適化、lint/型検査の新規整備。
A2未完了、B1未承認、SSH255原因分類Hを維持します。実環境の安全性を合成PASSから推定しません。

## 今回の検証記録

- バックエンド：全モジュール実行と最終変更関連の再実行、新規対応表試験を重複除外して
  **896件＝731 PASS・0 FAIL・0 ERROR・165 SKIP**。
- 前回の選択対象433件に新規32件を加えた範囲は、**465件＝360 PASS・105 SKIP**。
  既存PASSをSKIPへ変更していない。
- 全体のSKIP内訳はLinux固有143、PostgreSQL固有21、Docker opt-in 1。
  前回より60件増えた理由は実行対象の拡大（Linux40、PostgreSQL19、Docker 1）。
- フロントエンド：**68 PASS**（前回59＋新規9）。通常buildと合成デモ専用buildは成功。
- 新規拡張：**10 PASS**。コマンド・確認・dirty・キャンセル・本文非取得をモック検証。
- 合成メンバー2人の製品API/RPC/Git/DB接続試験と、製品画面モデルによる操作試験がPASS。
- Python構文、git差分・新規ファイル空白、gateway生成と不正ID拒否、Compose YAML構文を確認。
- 既知の通常buildのチャンク警告は残存。lint・型検査コマンドは未設定。
- ブラウザーでの目視・実code-server・実Linux・実PostgreSQL・Docker起動は今回実施していない。
