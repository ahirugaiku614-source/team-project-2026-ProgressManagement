# Stage 10-B／10-C：固定した保存済み変更への明示同意

## 現行仕様と従来方式との差

方式Cの採用承認を受け、未保存バッファの不存在を提出条件から外した。保存済みファイルを安定収集し、TaskSubmissionの不変Git commitへ固定する。その内容の一覧・差分と確認IDを表示し、明示提出で既存ChangeRequestへ変換する。確認後に保存内容が変わっても再収集しない。未保存本文取得、強制保存、保存だけによる提出・承認・統合は追加していない。

旧方式はVerifiedSession.saved=Trueと独立authorityの証明を必要とする。新方式は別型SnapshotSessionで、saved・dirtyという値自体を持たない。現在の認証済み利用者、Team所属、Task担当、登録対象、作業基準、workspace、保存元世代、DBセッション期限・失効を確認する。セッションIDの存在だけで許可せず、別基準への転用も拒否する。オンラインの全エディターや全ウィンドウの存在を証明するものではない。

011_saved_snapshot_sessions.sqlはセッションのcapture_policyを追加する。既存行はEDITOR_SAVED_V1のまま、新しいAPI発行セッションだけをSAVED_SNAPSHOT_V1にする。旧方式を常時成功へ変えない。実DBには未適用。

## 認可・処理順序

1. 既存Firebase認証・Team/Project/Task認可で管理側の作業基準を選ぶ。
2. 管理DBで方式Cのセッションを発行し、基準・workspace・世代へ結合する。期限切れ・失効・担当や登録対象変更を拒否する。
3. 明示的な収集要求でpermitとrequest IDを消費し、WorkCaptureBindingを収集前に永続化する。
4. BackendAuthorizationIssuerは現在の認可とDBセッションを検証し、version=2／COLLECT_SAVED_SNAPSHOTの別用途で最大30秒の証拠を署名する。通信HMACとは別鍵。提出ID、request ID、対象、セッション、世代、作業基準、workspace、収集サービスを束縛する。
5. 収集側は署名・期限・固定target・generation・audience・workspaceを照合してから永続台帳へ受付を記録する。旧version=1は従来の独立保存確認を維持する。HTTPで方式や任意rootを選択しない。
6. RO mountからFD相対に収集し、不変commit・ref・manifestを保存する。元の保存済みファイルを実行しない。
7. 確認APIはそのcommitだけから差分を生成する。確認IDの入力digestに利用者・Team/Project/Task・基準・収集commit・タイトル・削除証拠を束縛する。
8. 明示提出時に現在の認可・セッション・確認期限と同じ証拠を再照合し、基準を親とするレビューcommitとChangeRequestを一度だけ作る。workspaceは再収集しない。

同じ確認・収集・要求の再照会は同じ結果を返す。異なるタイトルや削除証拠、別の確認ID・収集IDの転用は拒否する。結果不明時は既存request IDで照会し、新しいIDで自動再提出しない。収集成功、レビュー依頼作成、承認、統合は別状態。

## 画面・拡張・最新版取得

画面は「保存済み変更を固定して確認」から一覧・差分を表示し、未保存の変更と固定後の変更を含まない旨を説明する。最終ボタンだけがChangeRequestを作る。タイトル等の変更で確認を無効化する。セッション認証を保存済み状態の証明とは表示しない。

拡張は未保存の変更を注意喚起するが、その本文を読まず、確認後に管理画面へのリンクだけを開く。dirty状態が変わっても未保存本文は送らない。従来の2試験の期待値変更はこの採用済み仕様によるもので、getText/save禁止は維持する。

最新版取得は既存の新世代作成を使用する。保存済み未提出変更がある場合は既存比較で拒否する。方式Cでは未保存の旧ウィンドウを証明できないため、新世代ができても旧workspace・基準・セッションを自動失効・削除しない。新しい画面へ旧session・確認ID・接続リンクを持ち越さない。既存作業のreset/checkoutや自動マージはしない。

## DB／Git競合

ReviewGitの共有Projectロック取得はfail-fastとした。DB行ロックを持つ経路がGitロックを待ち、Git保持経路がDBを待つ循環を継続しない。ProjectStorageの初期化だけは既存の有限待機を維持する。競合はStorageUnavailableで返し、同じrequest IDの照会・明示復旧に従う。

全処理のロック順序を一括変更したわけではない。実PostgreSQLの行ロック、Git失敗とDB確定、複数APIプロセスの競合はLinux試験で検証する。Git取得に失敗した収集受付を無条件再収集しない。

## 実接続factoryと既定拒否

snapshot_runtime.pyに固定設定専用provider・collector factoryを追加した。configured_snapshot_providerは管理側Git、既存の認可factory、SnapshotEditingSessions、固定UDS、既存の永続client ledgerを接続する。collectorはGit・DBを受け取らず、固定workspaceのversion=2証拠を検証する。

workspace_bootstrapの明示ASGI設定だけがcollectorsとgateway_enabledを受け取る。通常app.mainのget_submission_providerは拒否のまま。模擬providerへ自動fallbackしない。cookie gateではTeam付きFirebase依存を外し、既存のHttpOnly capabilityからDB上の利用者・セッションを特定する。通常APIのFirebase認証は外していない。

設定例はsettings-snapshot.example.json、配置追加はstage10c.yaml、Copilotへの成果物とコマンドはstage10c-handoff.mdを参照する。既存stage10bの比較記録にあるdirty必須の記述は採用承認前の履歴であり、現行仕様では本書を優先する。

## 未実証の境界

今回のLinux情報は利用者共有の実測でありCodexによる再接続ではない。Landlock ABI 8、pathname UDS/SO_PEERCRED、RO mount、tmpfsのumask077は確認されたが、製品結合成功ではない。実code-serverでの新規作成と既存編集・保存後の権限、rootless IDの対応、製品ワーカーのLandlock・期限・再起動台帳、011までのPostgreSQL移行と競合、実gateway失効は未実証。

実Linux・Docker・DB・Firebaseには今回接続しない。既存volumeを変更しない。A2未完了、B1未承認、SSH255原因分類Hを維持する。

## 最終検証と保全

- バックエンド全49モジュール・963件：794 PASS／0 FAIL／0 ERROR／169 SKIP。
- SKIP：Linux固有145件、隔離PostgreSQL23件、明示承認が必要なDocker診断1件。Linux情報共有を製品試験PASSに読み替えていない。
- 方式Cの接続18件とロック方針4件はPASS。未保存本文フィールドの拒否試験は実APIのextra=forbid・収集回数・固定内容の登録を確認するよう強化し、個別再検証もPASS。
- フロントエンド71 PASS、拡張10 PASS、成果物ツール12 PASS。通常・デモ専用ビルド成功。約638KBチャンク警告は残存。lint・型検査は既存コマンド未設定。
- 実装中の新規試験では、存在しないhead_commit参照、fixtureのmodel名、Mock内部RLockとの名前衝突を修正した。gateway結合で発見した不要なGit設定依存は製品側から除去した。最終全回帰はこれらの修正後の結果。従来の認可や拒否条件をテスト成功のために弱めていない。
- 変更候補39ファイル（追跡20、新規19）、Python22ファイルの構文・YAML/JSON構文・Git差分・秘密情報候補検査を実施。既存models.py末尾の空行は今回追加ではないため保持した。検査の限界があり、秘密情報の絶対不存在を断定しない。
- 旧worktree7ファイルは既存バックアップと直接バイト一致、バックアップ10ファイルと保護対象一時領域9件を保持。PR #21・#23、実Linux・実DB・実Firebaseは変更していない。
- HEADはf835b5c939ab2954081117b6d534b94f122ae449、ステージ0件。今回のStage 10-B追加分と方式Cの変更はcommit/pushしていない。

変更ファイルの正確な内容は転送ZIPのmanifestでも確認する。backend/appではセッション・署名用途・公開API・明示bootstrap・既定拒否を接続し、project_storage/review_git/task_reviewで競合時の非待機・一時障害時の状態保全を追加。011 SQLは未適用。frontendでは確認・提出文言と要求、世代切替時の状態破棄、合成デモを方式Cへ合わせた。labsでは新しい合成環境の設定、転送・展開・私有領域準備を整備した。
