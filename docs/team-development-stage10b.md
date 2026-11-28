# Stage 10-B：配置準備・合成実証の引き継ぎ

> 方式C採用前の調査・比較記録を保持しています。採用承認後の現行仕様と実行手順はteam-development-stage10c.md／stage10c-handoff.mdを参照してください。dirty必須という以下の記述を現行の方式Cへ適用しません。

## 現状と接続可否

以下は利用者から共有された実測情報で、今回CodexがLinuxへ接続して再確認したものではない。

- rootless Docker、runc、code-server 4.145.2。固定image digestはsha256:aaad9c5f5ecb70713188e8466060f186b08bd328cd3e1bcffc4a2a2dca33c14b。
- stage10-syn-demo-a / stage10-syn-demo-bは停止済み。A/B別volume・network・認証。コンテナ内UID/GIDは双方1000:1000。
- 保存先/home/coder/project。Ubuntuの18081/18082をWindowsの28081/28082へSSH転送し、ブラウザーで編集・保存できた。
- 合成demo.txtは所有者1000:1000、モード666。各1.5GiB・0.75CPU。

ブラウザーからの保存成功と、提出バックエンドへの安全な接続成功は別である。現在の666はsubmission_collection.pyのmode & 0o022で拒否される。親ディレクトリのモード、実volume名、rootlessのホストUID対応、mount IDは未確認。双方1000であることを学生間分離の証明にしない。runcは合成専用でありgVisorと同等ではない。

## 配置を二段階に分ける

### 第一段階：既存volumeの非破壊監査

stage10b-volume-audit.yamlを独立して使う。既存volumeをexternal・read-only・nocopyで各一つだけmountし、networkなし・非root・権限削減で明示した合成ファイルのメタデータだけを確認する。本文、認証情報、環境変数全体は取得しない。既存コンテナのcomposeへ追加しない。

本検査は666を安全に変える処理ではない。666のままならREFUSEDが期待結果。既存volumeのchmod、chown、コピー、削除、down -vは行わない。検査用プログラムの結果だけではdirty状態やファイル全体の安全性を保証しない。

### 第二段階：新規管理領域での接続検証

| サービス | 保存・権限 | 通信と責務 |
|---|---|---|
| 管理API | 管理専用Git rootと専用DBだけ | TLS gateway経由の認証、認可証拠発行、Git登録。通常providerは拒否 |
| PostgreSQL | 新規合成DB volume、既存DBと別資格情報 | manager networkのみ。公開portなし |
| 初期化worker A/B | 各専用0700 root、0600台帳、用途別鍵 | network:none、UDS、固定対象だけ。既存領域は初期化しない |
| code-server A/B | 初期化されたw-UUID一つだけをRW mount | 個別network・認証。Git、DB、管理root、鍵、Docker socketを渡さない |
| 収集worker A/B | 対応するw-UUIDをRO、各専用台帳 | network:none、UDS、IsolatedRootCollector。Git/DBなし |
| nginx / session gateway | 管理設定・TLSだけ | workspace固定ルート、HTTP認可、継続WebSocket失効監視 |

既存compose.yaml + stage10.yamlを新規専用project名で検査する。既存volume監査の構成とは混ぜない。新しいimageには今回の未コミット検証コードも必要なので、配置前に版を固定する承認を受ける。

UIDはDBのメンバーIDと区別する。A/Bを異なるOS UIDにする方式が第一候補。既存の双方1000を使う監査では、別コンテナ・PID・volume境界だけを確認し、ホストUID分離を達成したとは扱わない。rootless user namespaceの対応をメタデータで確認する。workerと対応するファイル所有者のUID/GIDが一致しなければ停止する。

設定・鍵・台帳の親は専用所有者0700、ファイル0600。UDS親だけはワーカー所有0770、socket0660とし、管理APIに接続用補助groupを与える。接続時のprimary UID/GIDはSO_PEERCREDで固定照合する。A/Bのsocket、台帳、通信要求鍵、応答鍵、認可鍵を分離する。初期化用と収集用も同じ鍵を再利用しない。秘密値を文書・出力へ記載しない。

収集サービスの起動には既存configured_serverへ固定target・generation・CollectorAuthorizationVerifierと信頼できるsession verifierの明示注入が必要。CLIへ任意import名や常時許可を追加しない。現状のDeniedSessionVerifierのままでは認可済み収集は成立しない。この未解決点を、設定だけで解決したとは扱わない。

新規code-serverでは管理されたlauncherでumask 077を指定し、合成ファイルを新規保存して0600または0644・ディレクトリ0700等を確認する。imageのENTRYPOINTを調べずcommandへshell文字列を足さない。実行可能ファイルの固定パスを確認してからexecする。既存666の修正は別途対象と承認が必要。利用者が後から書込み権限を広げた場合も収集器は拒否を維持する。

## Copilotへの作業指示（実行は別途承認後）

1. 停止済みA/Bを起動せず、Docker context・rootless/runtime・対象image digestを確認する。inspectはName、State.Status、Config.User、MountsのType/Name/Destination/RW、NetworkSettings.Networksの名前だけに限定する。全inspect、Env、認証設定本文を出さない。volume名と宛先/home/coder/projectの対応を記録する。
2. 上記volume名をSTAGE10_EXISTING_A_VOLUME/B_VOLUMEへ固定し、SYNTH_A/B_UID/GID=確認済み値、PROBE=demo.txtとする。値は対象管理設定で保持し、実ユーザーの領域は使わない。
3. 新規隔離作業ディレクトリで次を順に実施する。config出力に秘密値を混ぜない。

```sh
docker compose -f stage10b-volume-audit.yaml --profile approved-synthetic-audit config --quiet
docker compose -f stage10b-volume-audit.yaml --profile approved-synthetic-audit run --rm --no-deps audit-a
docker compose -f stage10b-volume-audit.yaml --profile approved-synthetic-audit run --rm --no-deps audit-b
```

4. 666拒否を記録する。合格させるためにchmodや検査変更をしない。新規管理root・台帳・鍵・TLS・合成PostgreSQLを別途準備し、既存データをmountしない。TLSの秘密鍵を報告しない。
5. 初期化台帳をWorkspaceJournal.provision、収集台帳をSQLiteReplayLedger.provisionで新規専用パスへ一回だけ作る。既存時は停止。workspace_bootstrapとstage10_asgiはStage 10-A記録の固定設定で起動する。初期化結果のUUIDとmount先を照合してから、新しいcode-serverの固定mountを設定する。自動配備は延期する。
6. 実セッションauthorityが未成立なので通常APIによる編集セッション発行・提出は拒否を期待する。合成認可器はテストプロセスだけに明示注入し、通常ASGI・製品画面へ登録しない。実利用者・実Firebaseには接続しない。
7. Linuxネイティブ試験は管理者所有0700の新規合成テストrootを指定し、backendから実施する。試験が作った新規領域以外を削除しない。

```sh
STAGE10_SYNTHETIC_NATIVE=approved STAGE10_SYNTHETIC_ROOT=/approved/new/private-root \
DATABASE_URL=sqlite:// python -B -m unittest discover -s tests -p test_stage10b_preflight.py
DATABASE_URL=sqlite:// python -B -m unittest discover -s tests -p test_managed_process.py
DATABASE_URL=sqlite:// python -B -m unittest discover -s tests -p test_submission_server.py
DATABASE_URL=sqlite:// python -B -m unittest discover -s tests -p test_workspace_runtime_rpc.py
```

8. 実SO_PEERCRED、Landlockによる初期化、socket親権限異常・symlink、RO mount、hardlink・別mount拒否を検証する。未対応kernelでfallbackしない。強制終了・親死亡・終了未確認・サービス再起動後の再利用拒否も確認する。既存試験の模擬FD/socketを実UDS確認済みと混同しない。
9. WebSocketはまず合成認証を注入する試験appと固定内部echo endpointで、実接続を張り失効・期限・バックエンド到達不能で切断されることを確認する。その後実code-serverのcookie名・path・subprotocolを検証する。通常ASGIのauthorityを常時許可にしない。直接18081/18082経路が残る間はgatewayを唯一の認証境界と主張しない。既存トンネルやコンテナの変更には別途承認が必要。
10. 新規合成PostgreSQLだけにテスト用資格情報を渡す。DB名はstage10_synthetic_で始める。test_stage10b_postgres.pyはSTAGE10_SYNTHETIC_POSTGRES=approvedとPROJECT_TEST_DATABASE_URLの両方を必要とし、自分専用のランダムschemaに旧基本テーブル・合成Userを作り、005～010を順に適用する。既存Userの保持、010の列、同じrequestの並行受付を検証する。今回このネイティブ試験は未実施。既存migration試験も同じ専用DBで実行する。001～010を既存DBへ流さない。空のスキーマへSQLだけを流す手順で代用しない。
11. 並行処理では同じrequest、初期化重複、認可失効、Git保存後のDB失敗・commit応答喪失を試験する。lock_timeout/statement_timeoutを専用接続で有限にし、DB→GitとGit→DBの同時取得を計測する。デッドロック・結果不明時は成功扱いせず提出を停止し、無条件再試行しない。全経路のロック順序改修は未実施。

起動・終了は専用projectと今回起動した対象に限定する。down -v、volume prune、既存socket unlink、旧台帳削除、既存A/B・PR #21への操作は指示しない。各項目の結果をPASS/FAIL/NOT_RUNで返し、本文や認証情報は添付しない。

通常stage10_asgiは既存Firebase認証を使用するため、今回の「実Firebaseへ接続しない」実証では、そのまま認証付き主要フローを起動しない。既存の合成API結合試験の明示依存注入を使う。常時許可する認証を製品bootstrapへ追加しない。完全なブラウザー主要フローには、後述のdirty仕様決定と合成認証付き実証appの配備準備が残る。

## dirty authorityの方式比較（仕様変更未承認）

| 観点 | A 管理された独立主体 | B code-server本体改修 | C 固定した保存済みスナップショット |
|---|---|---|---|
| 利用者の意図 | 全編集主体を登録できる範囲で未保存拒否 | 本体に到達した編集状態を検査 | 保存済み内容と提出予定差分を確認して同意 |
| 複数ウィンドウ・切断 | 全登録とheartbeat、失効が必要。未登録・offlineバッファは証明困難 | 本体改修でも未送信ブラウザーバッファの不存在は証明困難 | 未保存は含まれないことを明記。offlineウィンドウの不存在は証明しない |
| 確認後の変更 | 検査と収集の間の凍結・競合対策が必要 | 本体とFS収集の世代束縛が必要 | 内容IDを固定し、後の編集は次回提出へ分離 |
| 安全性 | UID/PID/debug/terminal境界と管理鍵が必要 | フォークの更新追随・改ざん防止が必要 | 任意パス禁止・認可・不変保存・容量・削除証拠は維持 |
| 工数 | 大。管理agentとエディター隔離の実証 | 最大。本体追随とクライアント連携 | 比較的小。既存TaskSubmissionと確認IDを活用 |
| 初心者の理解 | 「保存確認不能」で停止する理由が複雑 | 操作は単純でもoffline時の限界は同様 | 「保存済みのこの変更だけを提出」と説明しやすい |

MVPにはCを推奨する。ただし現時点では採用せず、dirty拒否を維持する。Cの承認には「すべての未保存変更がないこと」から「固定した保存済み内容への明示同意」への要件変更を明示する必要がある。

C案の順序は、認証認可→保存済み内容の安定収集→不変スナップショットと削除証拠を固定→その内容IDで差分表示→同じ内容ID・確認ID・タイトル等へ明示同意→既存ChangeRequest生成。確認後にworkspaceが変わっても再収集せず、固定内容だけを提出する。確認の更新は新しい収集IDを使い、旧証拠は保持する。切断時は同じrequest IDで読取り照会し、自動再提出しない。期限・失効・担当変更・main不一致時の拒否も維持する。現在のAPIが要求する実セッション検証を省略するコードは今回追加していない。

## 現時点の停止条件

666・不正所有者・不明なrootless対応・Landlock未対応・UDS権限不整合・証拠不一致・ロック不明・終了未確認は拒否。実providerとdirty authorityが未成立の間、実code-serverから通常の変更提出は利用不可。A2未完了、B1未承認、SSH255原因分類Hを維持する。Stage 10-B追加ファイルは承認までcommit/pushしない。

## 今回の検証・Git記録

Stage 10-Aの27ファイルはf835b5c939ab2954081117b6d534b94f122ae449で指定機能ブランチへpush済み。親は37b61d6ec56c3e8135e8acf739c5bee108c1c390。mainはf858ec8e18cf52a379b047ff6886a6522fd4721fのまま。PR #22のDraft・未マージと新HEADを確認した。PR #23は操作していない。

Stage 10-Aは直前の全回帰765 PASS／165 SKIP、フロントエンド68 PASS、拡張10 PASS、通常・デモビルド成功を再利用し、重複実行していない。

Stage 10-Bの追加試験は11件中7 PASS／0 FAIL／0 ERROR／4 SKIP。7件は666・664・所有者不一致・リンク・特殊ファイル等の合成属性検査。SKIPはLinuxネイティブ2件、隔離PostgreSQL2件であり実証済みではない。3 Pythonファイルの構文、YAML構文・RO/external/nocopy/network:none条件、新規ファイルの空白・秘密情報候補を検査した。パターン検査に検出限界はある。既存製品コード・安全条件はStage 10-Bでは変更していない。

追加5ファイルはapp/stage10_preflight.py、tests/test_stage10b_preflight.py、tests/test_stage10b_postgres.py（以上backend配下）、labs/team-development/stage10b-volume-audit.yaml、本書。ステージ・commit・pushを行わず保持する。旧worktree7ファイルの直接バイト一致、バックアップ10ファイル、保護対象一時領域9件の保持を確認した。実Linux・Docker・DB・Firebaseには今回接続していない。
