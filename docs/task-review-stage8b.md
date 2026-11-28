# Stage 8-B：タスク限定収集から既存ChangeRequestへの内部接続

共同開発支援の変更提出を対象とし、新しい独立レビュー機能は作らない。
公開API・Vue・code-server・実providerを追加/有効化していない。
クラス/表/APIの一括改名は行わない。007 SQLは明示適用用で、実DBへ適用しない。

## 作業基準と寿命

TaskReviewService.startはProjectのGitロックと既存DB認可下でmainを確定する。
mainなしでは既存ReviewGit.main_commitの空基準を一度取得し、専用参照へ保持する。
workspace初期化は管理側アダプターへ委譲し、基準・対象・basis ID・workspace ID・保存元世代が
一致するWorkspaceInitialization証拠と独立verifyを要求する。既定DeniedWorkspacesは拒否する。
実領域の上書き・clone・checkout・resetは実装しない。基準OIDの後付けは禁止する。

work_basesはメンバー/Team/Project/Task、workspace/世代、登録パス、対応設定版、基準、規則版、
開始日時、状態を保存する。対応版は既存登録のID/対象/登録者/時刻の固定識別から算出する。
状態はACTIVE→ENDED/SUPERSEDED/REVOKED。ログアウトとは結び付けず、明示終了でも
Git参照・収集・レビュー・作業データを消さない。権限/担当/対応変更では新規提出を拒否する。
基準はrefs/work-bases/<basis-id>と不変basis記録に保持し、欠落を自動再生成しない。

## 収集との事前結合

従来TaskSubmissionはbasis/workspace/保存元世代を保持していない。そのまま変換すると別基準へ
流用できるため、work_capture_bindingsを追加した。認可済みPROCESSING・消費済みpermitの段階で
bind_collectionを呼び、信頼済みworkspace/session検証器で対応を確認してから収集する。
すでにSUBMITTEDの既存収集へ後付けしない。対応のない過去収集は変換拒否とする。
公開POSTへの自動接続は今回行わず、将来APIで受付→事前結合→収集の順を明示する必要がある。

## 削除と変換

管理側が基準treeのタスク内・非除外ファイルだけから不透明candidate IDを作る。
confirm_deletionはcandidate IDを再検索し、basis/メンバー/Project/Task/path/blob/mode/
対応版/規則版/確認操作/収集IDをtask_deletion_intentsへ固定する。任意パスを許可にしない。
提出時にも全項目を照合し、残存ファイルとの矛盾・別basis/収集への転用を拒否する。

基準のProject全treeを出発点とし、タスク内の追加・変更・明示削除だけを反映する。
タスク外、除外ファイル、既存モードは保持し、新規は100644とする。欠落に削除証拠がなければ
拒否する。ファイル/ディレクトリ置換は、必要な削除証拠を満たし、最終パス衝突がない場合だけ。
境界外削除・移動は扱わない。名称変更は明示削除と追加として表現する。
Stage 3の空収集拒否を維持するため、全ファイル削除だけの提出はこの初期版では拒否する。
この部分は既存契約との制約として停止し、全削除対応を勝手に追加しない。

生成レビューcommitの唯一の親は作業基準。収集commitやmainを書き換えない。
全体上限・commit/tree/blob検証を維持し、期待treeと生成commitを照合する。
変更なしならNO_CHANGEで確定し、ChangeRequestは作らない。

## 登録・障害復旧

task_review_conversionsは収集IDを主キーとし、基準・固定recipe・入力識別・状態・生成commit・
依頼ID・修正元依頼を保存する。request keyは専用namespaceで収集IDから導出する。
修正元は同じProjectかつ本人の依頼に限定し、過去のレビュー結果を変更しない。
入力recipeにはタイトル・削除証拠・基準・規則等を含め、同じ収集IDでの入力変更を拒否する。

PREPAREDをDBへ先にcommit→Git変換→不変conversion記録→既存review manifest/refs→
既存snapshot検証→ChangeRequestと対応表を同じDBトランザクションで確定する。
新規内部register_review_snapshotは既存経路とDB列対応を共有し、公開任意OID APIにしない。
既存submit_change_requestのworkspace収集・API契約は維持する。

READY/NO_CHANGE以外の再要求は通常拒否し、recover=Trueの明示内部操作だけが既存記録を照合する。
Git保存後のDB失敗ではcommit/参照/記録を保持し、同じキーで再登録する。収集元へ戻らない。
変換記録がないPREPARED、不一致・破損は推測で再生成しない。レビュー完了済みの記録欠落も
成功として返さない。収集成功、依頼作成、承認、統合は別状態である。

## 次の画面/APIと実環境条件

将来の画面は「作業を始める」「変更を確認」「削除を確認」「変更を提出」を内部サービスへ接続し、
既存一覧/詳細/差分/承認/修正依頼/統合APIを再利用する。保存だけでは提出しない。
mainが進んでもbaseは固定し、既存統合のbase不一致409を維持する。最新版取得/三方向比較/
競合解決支援は別工程。統合で他メンバーの作業を消す自動更新は追加しない。

LinuxのFD/参照ロック/同期、PostgreSQL並行ロック、実workspace初期化・session真正性は未検証。
テストは一時Git・SQLite・合成証拠であり実環境保証ではない。実providerは既定拒否。
A2未完了、B1未承認、SSH255分類Hを維持する。

## Windowsでの検証結果

2026-11-24に、新規34件とStage 1〜7-A・既存ChangeRequest関連の回帰テストを一括実行した。
発見349件、PASS244件、FAIL0件、ERROR0件、SKIP105件。新規34件はすべてPASSで、既存分はPASS210件・SKIP105件だった。
SKIP対象にはLinux専用のレビュー・統合・安全ストレージと、隔離PostgreSQLを必要とする試験が含まれる。
過去の209 PASS・7 SKIPとは対象範囲が異なるため、履歴を置き換えたり合算したりしない。
変更Python 5ファイルの構文解析とgit diff --checkもPASSした。

途中の新規31件の確認では、空基準互換性が1件ERRORとなった。試験用Gitの作成者・日時が既存統合契約と
異なっていたため、試験フィクスチャだけを既存形式へ合わせた。製品側の初回基準検証は緩和していない。
最終一括結果はこの変更後のものである。

旧worktreeの提出基盤7ファイルは既存バックアップとの直接バイト比較ですべて一致した。
実DBへのSQL適用、Linux操作、実provider有効化、ソースリポジトリへのcommit/pushは行っていない。

## 公開接続前に残る条件

現在の権限・担当・対応設定は処理ごとに再検証し、不一致を検出した作業基準は失効する。
ただし、確認と確認の間に担当や所属が変更され元へ戻された履歴までは、現行対応表から復元できない。
公開APIへ接続する前に、担当・所属・対応設定の更新側と連動する失効世代または失効イベントの契約が必要である。
作業開始のGit保持後にDB登録だけが失敗した場合も、孤立した基準証拠を自動削除・再生成しない。
実workspaceの初期化・セッション証拠、全削除専用の空収集契約、Linux/PostgreSQLでの排他・障害試験は別途必要である。
