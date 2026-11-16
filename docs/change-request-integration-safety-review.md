# PR #8 マージ前の安全性レビュー

対象は`feat/change-request-integration`の初期コミットba36a9fと、それに対する本修正。
確認・修正・試験は稼働backendのbind mount外の別worktreeで実施し、
既存PostgreSQL、Git保存領域、Dockerボリュームには接続・変更していない。
実DBに004を適用していない。PRをmainへマージする操作も行わない。

## 見つかった問題と修正

### 永続化失敗後の再送（重要）

mainのrename後、headsのfsyncに失敗すると、DBはPREPARED・mainはafterになる。
従来は再送で値がafterなら同期をせずCOMPLETEDにできた。
OS上で値が見えていてもディスクへの永続化が失敗したままなので、
成功応答後の電源断等でGitとDBが食い違う可能性があった。

`integration_git.sync_main`でOIDを照合し、正式mainファイルと親を必ず再fsyncする。
packed-onlyならpacked-refsとその親、loose不在のheadsを同期する。
activeの公開後やアーカイブのrename後にも同じ問題があり得るため、
`sync_active`・`verify_archive`でファイルと親の再照合・再同期を追加した。
同期できない間は503で止め、mainを巻き戻さずPREPARED等の記録を保持する。

### 完了済みというDB状態だけで成功していた（重要）

従来のCOMPLETED再送はDBとintentアーカイブの一致だけを見ていた。
mainが欠落・巻戻し・別履歴になった場合も200を返し、復旧が必要な状態を隠していた。
また、COMPLETEDと表示されたactiveを、対象Gitの検査前に退避していた。

main.lock下で`verify_completed_main`を実行し、現在mainから結果commitへ辿れることを
commit内容ハッシュとともに確認する。後続の正常な一親fast-forwardは許可する。
上限256段、参照欠落、別履歴、複数親、破損は安全に停止する。
activeの退避は提出内容・履歴検査後だけに変更し、他依頼による暗黙の退避は行わない。

### 古い準備記録による誤った初回・再公開（重要）

初回用baseの検査は新規準備時だけで、PREPAREDのNULL beforeを再送時に信用していた。
DB/Gitの部分復元等でNULL beforeと非空baseが組み合わさると、履歴を誤って初回扱いできた。
さらにDBだけがPREPAREDへ戻り、完了アーカイブが残っている状態では、再公開後に
アーカイブ競合で409になる場合があり、エラー応答でもmainが変わっていた。

初回用baseを再送でも検証し、既存Project統合履歴との矛盾と既存アーカイブを公開前に拒否する。
空tree・親なし・固定メッセージに加え、従来の生成処理のauthor/committerヘッダー形式も確認する。
DB・Git・記録が矛盾する場合は自動補正せず管理者確認とする。

### マイグレーションの同名索引検査

CREATE INDEX IF NOT EXISTSだけでは、同名の別列・別表の通常索引を見逃していた。
通常索引の対象表・列・有効性・非部分性も検査する。
主キー・操作ID一意制約の索引が有効・即時であること、FKが遅延されないことも確認する。
想定外ならトランザクションを中止し、既存索引・制約をDROPして自動補正しない。
004の正しい初回作成構造は変更せず、その再実行時検査を強化している。

## 依頼された8項目の確認結果

| 項目 | コード上の根拠・試験 |
| --- | --- |
| 1. main.lock〜replace | `locked_main`のO_EXCL、所有inode検査、直前read_ref、原子的rename。実Git update-refは拒否される。最終検査後のロック無視書き込みは防げないことも別プロセスで再現し、保証外として記録 |
| 2. replace後のfsync/DB失敗 | `sync_main`を公開後・PREPARED再送の両方で必須化。mainの再公開や巻戻しをせず同一操作を確定。file/parentのfsync障害とDB確定失敗を注入 |
| 3. PREPARED再送 | 現在の所属・承認のロック下再確認、`verify_snapshot`、`check_record`、初回base、DBとactiveの完全一致、アーカイブ不存在、実mainのbefore/after照合。main変更前にactiveも再同期 |
| 4. COMPLETED再送 | `verify_completed_main`で現在mainへの到達性とcommitハッシュを確認後、`verify_archive`で記録と永続性を確認。欠落・巻戻し・無関係な履歴・破損・探索上限を拒否 |
| 5. 仮想base | `verify_initial_base`で内容ハッシュ・空tree・親なし・固定メッセージ・生成ヘッダーを検証。main_commitを再実行しない。正常な初回の親子履歴も維持 |
| 6. 004再実行 | 正しい構造・旧提出/承認/統合行を保持。異なるCHECK、通常/部分索引、遅延一意性を拒否。新規一時PostgreSQLで実適用・再実行し、異常構造を削除しないことを確認 |
| 7. DB/退避直後の停止 | fork+os._exitでPREPAREDコミット直後・COMPLETEDコミット直後・アーカイブ退避直後に実終了。DB/ファイルの段階が正しいこと、残存ロックは自動削除せず503で停止することを確認。通常のロック解放後の応答消失は200で再送可能 |
| 8. 既存機能 | 提出・レビュー・状態の定義、個人workspace/.git、他Project、既存レビュー参照を変更しない。既存回帰テストと不変性検証を再実行 |

## 保証範囲と残る制限

値の読み直しとos.replaceは、一つのハードウェアCAS命令ではない。
main.lockを守る通常Gitライターを排除する間だけ、CAS相当の更新を保証する。
最終値検査とrenameの間に直接書くプロセスの値は上書きされ得る。
その窓をテストで実際に再現しており、「再検査すれば任意の改ざんも防げる」とはしない。
共有保存領域のバックエンド専有と、全ライターによる標準ロックの遵守が前提。
renameがmain.lockを消費した後には通常Gitも次の更新を開始できる。
本API同士はProjectのflockで直列化し、外部Gitの操作は運用で停止・調整する。

fsyncの再実行はOSが報告した失敗への対策であり、ストレージが永続性を偽る場合まで保証しない。
実電源断・ストレージ故障は未検証。複数ホスト/NFS、ロック無視の改ざん、本番エディタの隔離も対象外。
強制終了で残るGitロックは削除しない。管理者が全ライターの停止と記録を保全・照合する必要がある。
COMPLETEDが現在mainの祖先でも、後続commitの全tree/blobのfsckまでは行わない。
対象提出のbase/headは従来の全内容検査を通し、追加の到達性検査では経路上のcommitを検査する。

## 再現と回帰検証

修正前ba36a9fに4件の回帰テストだけを追加して実行し、以下すべてが失敗することを確認した。

- 完了後にmainが消えても200を返す。
- rename後のfsync失敗の再送で、正式mainファイルのfsyncが失敗しても200を返す。
- 非空baseを持つ初回PREPAREDの再送を許可する。
- PREPAREDと完了アーカイブが食い違う場合、409応答前にmainを書き換える。

修正後の検証は専用PostgreSQL 15のtmpfsと専用ネットワーク、一時Git、
独立したLinuxコンテナだけを使用する。実Firebase通信は既存テストと同様に代替する。
既存DBへのDDL、既存Git/ボリュームの初期化や削除は行わない。

### 最終結果（2026年11月16日）

従来184件に回帰17件（統合15件、移行2件）を追加し、合計201件。

| 環境 | 成功 | スキップ |
| --- | ---: | ---: |
| Linux / PostgreSQL 15 | 200 | 1 |
| Linux / SQLite | 173 | 28 |
| Windows（既存の仮想環境） | 40 | 161 |

PostgreSQLの1件はコンテナにDocker CLIがないためのCompose検証で、Windowsで成功。
SQLiteはPostgreSQL専用27件もスキップ、WindowsはLinux/PG専用をスキップしている。
統合・移行だけの57件もPostgreSQLですべて成功。Dockerビルドとgit diff --checkも成功した。
既存TestClientの非推奨警告は残るが、テスト失敗はない。
