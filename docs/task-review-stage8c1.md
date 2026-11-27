# Stage 8-C1：タスク変更の公開入口と作業基準の失効

目的は共同開発の変更提出を既存ChangeRequestへつなぐことである。独立したレビュー機能は追加しない。
保存だけでは収集・変更提出・承認・統合を開始しない。実provider、実セッション検証器、実workspace初期化検証器は拒否のままである。

## APIとVueからの呼出し順序

共通prefixは `/api/teams/{team_id}/projects/{project_id}/tasks/{task_id}/work-bases`。
全入口で既存Firebase認証、Team所属、ProjectとTask対応、現在の担当を検証する。
basis IDを扱う場合は、さらに本人・Team・Project・Taskとの一致を検証する。

| メソッド・suffix | 操作・入力 | 主な出力 |
|---|---|---|
| POST prefix | 作業開始。本文は `{}` のみ | id、state、started_at |
| GET `/current` | 本人の最新作業基準を確認 | basis、存在しなければnull |
| GET `/{basis_id}` | 基準の状態確認 | id、state、started_at |
| GET `/{basis_id}/deletion-candidates` | 基準treeから削除候補を確認 | 不透明なcandidate ID、Project相対path |
| POST `/{basis_id}/deletion-intents` | submission_id、candidate_id、明示確認操作のconfirmation_id | deletion_id、相対path |
| POST `/{basis_id}/collections` | 既存permit、request_id、editing_session_id、has_unsaved_changes | submission_id、collection_state、error_code |
| POST `/{basis_id}/preview` | submission_id、title、任意description、deletion_ids、任意previous_request_id | confirmation_id、有効期限、変更一覧、除外一覧、no_change、main_differs |
| POST `/{basis_id}/change-request` | previewと同じ入力＋confirmation_id | 収集・変換・レビュー・統合の各状態、ChangeRequest ID |
| GET `/{basis_id}/collections/{submission_id}` | 本人の収集と対応結果を照会 | 収集・変換・レビュー・統合の各状態、要確認フラグ |

Vueは「作業を始める」→保存→既存submission-permitsで短命許可を取得→新collectionsで明示収集→
削除候補と明示削除証拠の確認→previewで変更一覧の確認→confirmation_id付き明示提出→結果照会の順に呼ぶ。
previewは本文を返さない。コード差分の閲覧・承認・修正依頼・統合は従来のProject配下
`change-requests` 一覧・詳細・diff・approve・request-changes・integrateを再利用する。
今回はVueやcode-server提出用拡張を実装しない。

## 確認と提出の分離

previewは保存済みGit証拠・削除証拠・作業基準から変換入力を構成する。
ChangeRequest、レビュー用commit、レビューref、main更新は作らず、本文を保持しない確認レコードだけを保存する。
確認IDは10分間有効で、基準・収集ID・正規化されたタイトル/説明・削除証拠・前依頼との対応に束縛する。
提出時には同じ証拠を再読取りし、入力が違えばPREVIEW_CHANGEDで拒否して再確認を求める。
確定済み同一入力の応答確認は、確認期限切れでも既存変換を再照合し、再登録しない。

収集時とpreview・提出時に、信頼済みセッション検証器が対象・session・保存元世代・期限・保存済み状態を確認する。
利用者申告のhas_unsaved_changes=falseだけでは通過しない。未保存なら中止し、強制保存や未保存バッファ送信はしない。
実検証器が未接続ならEDIT_SESSION_UNVERIFIEDで拒否する。
既存の収集結果だけから変換し、再収集や別キーでの自動再提出を行わない。明示復旧は従来の内部管理操作だけに残す。

## 不可逆失効と競合

008 SQLのDBトリガーで、関連更新と同じトランザクション内でACTIVEな基準をREVOKEDにする。
担当・所属・対象フォルダーが一時的に変わり元に戻った場合も失効は残る。ロールバック時は更新と失効を一緒に戻す。

対象はTask担当・所属Team変更、Task終了(DONE)・削除、提出対象対応の変更/削除、
Team所属の削除/役割・識別情報変更、Project所属Team変更、保存元世代変更/削除である。
既存のProjectアクセスはTeam所属に基づくため、その権限境界を利用し、存在しない独立ACLを追加しない。
Taskの題名変更やTODO→IN_PROGRESSだけでは失効しない。

保存元世代は管理側のadvance_workspaceだけで増加更新する。HTTPから世代やOIDを指定できない。
startの検証済み初期化証拠から世代を登録する。世代変更は旧基準を失効させ、確認・提出時には独立初期化証拠も照合する。
基準の寿命をログインsessionとは分離し、ログアウトだけで履歴やファイルを削除しない。
失効後も収集commit、ChangeRequest、既存のレビュー判定、ファイルを削除しない。

Git変換PREPAREDのDB確定後は認可ロックが解放されるため、ChangeRequestのDB確定直前にも現在の基準・認可を再検証する。
トリガーと認可ロックの実PostgreSQLでの待機・デッドロック・並行動作は別途検証が必要である。
失効トリガー未適用の環境では新公開機能を拒否する。自動でDDL適用・補修しない。

## DB変更・空収集・障害

008_task_review_api.sqlを作成したが実DBには適用していない。
work_workspace_revisions、work_review_previews、work_collection_diagnosticsと9個の失効トリガーを追加する。
既存作業基準の世代は移行時に引き継ぐが、移行前の失効履歴を保証できないため旧ACTIVE基準はREVOKEDにする。
workspaceやGit refを再生成・削除せず、新しい基準は別途検証済み初期化証拠から確定する。
SQLite合成テストは新規テストからの明示呼出しだけで同じ失効条件のトリガーを作る。
metadataイベントやcreate_allからは作らない。通常起動のcreate_startup_tablesは追加表を除外するため、移行は暗黙適用されない。

全削除による空収集はEMPTY_CAPTURE_UNSUPPORTEDで拒否し、NO_CHANGEと区別する。
読み取りに失敗したものや除外対象を削除に変換しない。一部削除はStage 8-Bの証拠契約を維持する。
表現できない空treeはUNSUPPORTED_EMPTY_TREEで拒否する。
収集障害では収集行をREVIEW_REQUIREDにし、固定分類だけを診断表へ保存する。本文・鍵・内部例外は保存しない。
遠隔RPCが失敗分類を証明できない場合はCOLLECTION_REVIEW_REQUIREDとし、空収集などの原因を推測しない。
202は受付を意味し、収集成功やレビュー依頼作成成功とは扱わない。

Git保存後のDB失敗は既存証拠を保持する。結果照会で収集状態、変換状態、review_requiredを別々に返す。
main_differsがtrueでも基準を変更しない。既存統合はbase不一致を拒否し、自動マージしない。

## 応答と固定エラーコード

Cache-Controlはno-store。本文・OID・ref・絶対パス・内部例外は新APIの応答に含めない。
error応答は `detail.code` を日本語画面で説明する。たとえば以下を使う。

| HTTP | code | 画面での説明例 |
|---|---|---|
| 401/403 | AUTHENTICATION_REQUIRED / REVIEW_FORBIDDEN | ログインまたは担当・所属を確認してください |
| 404 | WORK_BASIS_NOT_FOUND / COLLECTION_NOT_FOUND / REVIEW_TARGET_NOT_FOUND | 対象を利用できません |
| 409 | WORK_BASIS_INACTIVE / TASK_FINISHED | 旧作業基準は利用できません |
| 409 | PREVIEW_CHANGED / PREVIEW_EXPIRED | 変更内容をもう一度確認してください |
| 409 | UNSAVED_CHANGES | ファイルを保存してから確認してください |
| 409 | EMPTY_CAPTURE_UNSUPPORTED / UNSUPPORTED_EMPTY_TREE | 初期版では全削除・このtree形式を扱えません |
| 409 | REVIEW_EVIDENCE_CONFLICT / COLLECTION_BASIS_CONFLICT | 証拠が一致しません。別IDで自動再提出しないでください |
| 413/422 | REVIEW_LIMIT_EXCEEDED / INVALID_REQUEST | 上限または入力を確認してください |
| 503 | EDIT_SESSION_UNVERIFIED / REVIEW_SERVICE_UNAVAILABLE | 検証基盤未接続または障害のため処理できません |

結果のreview_state・integration_stateは既存DBから読み、独立した判定を追加しない。
collection_state=SUBMITTEDでもconversion_stateがREADYとは限らない。READYでも承認・統合完了とは限らない。
結果照会は既存詳細APIと同様に記録済み状態を返し、新たな収集や統合を実行しない。
現在のGit内容の照合・差分閲覧は既存diff API、登録・復旧の証拠検証は内部変換サービスが担当する。

## 前回105 SKIPの分類

Windows環境での定義を調査した結果、Linux固有103件（安全ストレージ5、レビュー収集28、レビュー判定22、統合48）、
隔離PostgreSQLを要する動的SKIP2件だった。後者はPROJECT_TEST_DATABASE_URL未設定によるProject名移行試験である。
前回7件はLinux5件＋PostgreSQL2件で、追加された98件は既存ChangeRequest関連を実行対象へ拡大した分である。
依存ライブラリ不足によるSKIPや、条件を緩めたことによる減少はない。対象4テストファイルのSKIP定義も変更していない。
Linux条件で分類した103件のうち6件にはPostgreSQL条件もあるが、105件の集計では重複計上しない。
実行対象の追加分には98 SKIPのほか、Windowsで実行できる既存ChangeRequestメタデータ試験1件がある。
したがってStage 7-Aの216件に、Stage 8-Bの34件と既存ChangeRequest関連99件を加えたものが前回349件に対応する。
過去の個々のPASS IDを記録した完全な一覧は保持していないため、個別IDの履歴照合済みとは扱わない。
同じWindows条件における増加は上記の実行範囲拡大で説明でき、過去のPASSを新しいPASSとして流用しない。

## 実環境で未検証の事項

PostgreSQLの008 SQL・トリガー実行・行ロック・障害復旧、LinuxのFD/参照ロック/同期、UDS、実workspace初期化、
編集セッション/dirty真正性は実環境未検証である。合成SQLiteトリガーと一時Gitの成功はその保証ではない。
実providerを有効化せず、PR #21環境、旧worktree、既存バックアップを変更しない。
A2未完了、B1未承認、SSH255分類Hを維持する。

## 検証中に修正した回帰

初回一括388件は282 PASS、0 FAIL、1 ERROR、105 SKIPだった。
ERRORは既存の部分schema作成・繰返しcreate_all試験で、SQLiteトリガー自動設定が存在しないProject表を参照したためだった。
metadataイベントを廃止し、新規合成テストだけからの明示設定へ限定した。既存試験の定義・期待値は変更していない。
修正後の関連19件（失効・移行時の切替・既存の段階的schema作成・API開始）はすべてPASSした。

## 最終Windows合成検証（2026-11-25）

修正後に全対象を再実行し、発見390件、PASS285件、FAIL0件、ERROR0件、SKIP105件だった。
新規Stage 8-C1は41件すべてPASS。既存Stage 1〜8-B・ChangeRequest関連は244 PASS・105 SKIPである。
内訳はLinux安全ストレージ5、Linuxレビュー収集28、Linuxレビュー判定22、Linux統合48、
隔離PostgreSQLが必要なProject名移行2で、上記の前回分類と同じだった。
追加・変更Python 8ファイルの構文解析とgit diff --checkもPASSした。
過去の209 PASS・7 SKIPやStage 8-Bの結果は別の履歴として保持する。

旧worktreeの提出基盤7ファイルは既存バックアップと直接バイト比較ですべて一致し、バックアップ10ファイルを保持した。
作業ブランチはfeature/saved-folder-submission、HEADはf858ec8e18cf52a379b047ff6886a6522fd4721fのまま。
旧ブランチのHEADも5a7fac0872af55db2fb7355d419cacbdd8eae7cdのままである。
commit・push・PR作成・実DB移行・Linux環境操作・実provider有効化は行っていない。
