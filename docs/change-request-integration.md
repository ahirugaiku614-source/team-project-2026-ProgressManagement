# 承認済み変更の共有mainへの統合（第6段階）

## 対象と処理の意味

承認済みの固定スナップショットを、Project専用の共有bare Gitのmainへ設定する。
自動マージ・rebase・競合解決はしない。GitHub上のこのアプリのmainを操作するAPIでもない。
個人ワークスペースとその.git、提出時のcommit・参照・復旧記録、承認記録は変更しない。
Vue、code-server、個人作業領域への最新版取得は対象外。

既存のOPEN/CLOSEDとPENDING/APPROVED/CHANGES_REQUESTEDは従来の意味を維持する。
統合は別テーブル`change_request_integrations`のPREPARED/COMPLETEDで管理する。
統合しても依頼をCLOSEDにせず、承認を別の判定に書き換えない。

## API

全てFirebase IDトークンを`Authorization: Bearer ...`で送る。
パス中のTeam、Project、依頼の対応と現在の所属を確認する。
統合者IDはFirebase UIDに紐づくUserから取得する。
提出者本人も、別メンバーが承認済みなら統合できる。

### POSTで統合・復旧

```text
POST /api/teams/{team_id}/projects/{project_id}/change-requests/{change_request_id}/integrate
```

本文は不要。`{}`も許可する。ユーザーID・操作ID・パス・コマンド等の指定は422で拒否する。
成功は200。初回と、同じ操作の復旧・完了済み再送は同じoperation_idを返す。
成功レスポンス例：

```json
{
  "change_request_id": 12,
  "project_id": 3,
  "operation_id": "cf9e4cd2-0695-4cdf-872b-a03e87c621e7",
  "integrated_by": 2,
  "status": "COMPLETED",
  "prepared_at": "2026-11-16T10:00:00",
  "integrated_at": "2026-11-16T10:00:01",
  "main_before": "1111111111111111111111111111111111111111",
  "main_after": "2222222222222222222222222222222222222222"
}
```

コミット値とIDは例。日時はUTC。prepared_atは準備記録の作成日時、integrated_atは
Git更新を確認してDB確定した日時で、障害復旧時には最初のGit更新より後になる。
初回で実際のmain参照が存在しなければmain_beforeはnull。仮想baseのIDとは区別する。

### GETで操作記録を確認

```text
GET /api/teams/{team_id}/projects/{project_id}/change-requests/{change_request_id}/integration
```

同じ認証・所属確認を行い、準備前はnull、準備後は上記形式の記録を返す。
PREPAREDではintegrated_atがnullになる。
**PREPAREDは「Git未更新」を意味しない。既にmainが更新済みの場合もある。**
GETはDBの記録だけを読む。Gitの現在値の検証・復旧はPOSTで行う。
既存の提出・一覧・詳細・差分・レビューAPIのレスポンスは変更しない。

## 統合できる条件

- 依頼が対象Projectに属し、認証ユーザーが現在の所属メンバーである。
- 依頼はOPEN、別メンバーによる判定はAPPROVED。未判定・差し戻し・CLOSEDは409。
- 他の未確定統合が同じProjectにない。
- DB、提出用JSON、loose/packedのレビューbase/head参照が一致する。
- base/headのcommit・root/中間tree・blobの内容ハッシュ、親commit、パス・モード・サイズが正しい。
- 現在の実mainが提出時baseと一致する。例外は次節の初回統合だけ。

mainが先に進んでいた場合、今回のAPIは変更を取り込もうとせず409で停止する。
後続段階の同期・再提出が必要であり、現段階でそれを自動実行しない。

## 初回mainと仮想base

既存のReviewGit.main_commit()はmainがなければ「Empty review base」という
親なし・空treeのcommitを作る。生成日時が入るため、同じ内容でも再生成したIDとの比較はできない。
提出APIの仕様は変更せず、統合時はmain_commit()を呼ばない。

実mainがない初回は、保存済みbaseの内容ハッシュ、空tree、親がないこと、固定メッセージを確認する。
提出commitの唯一の親がそのbaseであることも既存の検証で確認する。
その提出commitをmainにするため、空ルート→提出commitという二つのcommitの履歴が残る。
baseを作り直す、親を切り離す、提出スナップショットを作り替える処理はない。
既に同Projectの統合履歴があるのにmainが消えていた場合は初回扱いせず停止する。

## 排他制御とGit参照の公開

ロック順はProjectの既存flock→DBのProject/所属/依頼/承認→Gitのpacked/base/head→main。
Projectと所属は共有行ロック、依頼はFOR UPDATE、承認は共有行ロックで読み直す。
PostgreSQLの行ロック待ちはトランザクション限定で5秒に制限する。
準備コミットでDBロックが解放されるため、公開前に所属・対応・状態・承認・提出内容を再検査する。

packed-refs.lockとレビューbase/headの.lockには既存処理を再利用する。
main.lockもO_EXCLで取得し、他者の既存ロックは削除しない。
新しいOIDをmain.lockへ書いてfsyncし、現在の実mainをもう一度検査する。
期待値と一致した場合だけmain.lockをmainへ同一ディレクトリ内でrenameし、親をfsyncする。
これがcompare-and-swap相当の公開点となる。
未使用ロックの解放時もinode・種類・リンク数で所有権を確かめ、置き換わった他者のロックを削除しない。

mainは更新される参照なので、looseがあればGitと同じくそれを優先する。
packedに以前のmainが残る通常の状態を不整合とは扱わない。
packedだけのmainを更新してもpackedを編集せず、loose mainを公開する。
不変のレビュー参照については従来どおり両形式を照合し、不一致なら停止する。

Gitコマンドは既存ReviewGitのshell=False・引数配列・隔離設定・プロトコル禁止を使う。
参照の更新自体は検証済みディレクトリFDで行い、共有config・フック・外部リモートを使わない。
シンボリックリンク、hardlink、特殊ファイルは既存のO_NOFOLLOW・通常ファイル検査で拒否する。

## DBとGitの二段階処理

1. 権限・承認・スナップショット・現在のmainを検査する。
2. 操作IDをサーバーで発行し、依頼・Project・開始者・期待するmain前後をPREPAREDとしてDBへコミットする。
3. 権限・承認と提出内容をロック下で再検査する。
4. Projectのintegration-journal/active.jsonを不変ファイルとして公開・fsyncする。
5. CASでmainを更新する。
6. DBをCOMPLETEDへコミットし、確定日時を記録する。
7. activeを操作ID.jsonへ上書きなしrenameして保存する。記録は削除しない。

主キーは依頼ID、operation_idも一意。同じProjectにPREPAREDは一件だけという部分一意索引を持つ。
既存の依頼状態を更新しないため、統合済みかどうかは必ずこの専用記録で判断する。
DBだけ、mainだけを見て処理済みと推測しない。

ジャーナルには準備時点の操作ID・Project・依頼・開始者・日時・main前後だけを保存する。
DBの不変な準備値と完全一致しなければ自動復旧しない。
DBが完了済みでactiveの退避だけが失敗した場合は、再送または次の統合で退避を再実行できる。
DBに対応しないactiveがある場合は、別依頼も含めて停止する。

## 障害と再送

| 停止・失敗の位置 | 残る状態 | 同じPOSTの動作 |
| --- | --- | --- |
| 準備DBコミット前 | main未更新、DB記録なし | 条件を再検査して新規開始 |
| 準備DBコミット後、active作成前 | PREPARED、main更新前 | 期待値一致なら同じ操作IDでactiveを作成 |
| active作成後、Git更新前 | PREPARED＋active、main更新前 | CAS条件が一致する場合だけ再実行 |
| Git更新後、DB確定前 | PREPARED＋active、main更新済み | 現mainが保存済みafterと一致すればGitを書かずDBを確定 |
| DB確定後、active退避前 | COMPLETED＋active | Gitを書かずactiveを退避して同じ結果を返す |
| 応答消失 | 記録に応じる | GETで確認し、同じPOSTで再検証 |

mainがbefore/afterのどちらでもない、記録がない・一致しない場合は409で停止する。
Git更新後のDB失敗でもmainを巻き戻さない。既存Gitオブジェクト・レビュー参照を削除しない。
COMPLETEDの再送は、後続の統合でmainが進んでいても同じ記録を返す。
ただし現在も所属・OPEN/APPROVEDであることと、提出内容・保存記録の検証は必要。

PREPAREDの再開は最初に準備した本人だけが可能。別メンバーによる引き継ぎは409。
完了済み結果の再送・閲覧は現在の所属メンバーに許可する。
元の統合者が所属を失った場合などは、権限を回避せず管理者対応が必要。
未確定操作が残っている間、別依頼を先に統合して復旧根拠を失わせない。

### 強制終了・残存ロックの扱い

プロセスの強制終了ではfinallyが走らず、Git標準ロックが残ることがある。
この場合APIは503で止まり、ロックを強制解除しない。自動復旧できたとは扱わない。
管理者は関連する全Git書き込みプロセスを停止・確認し、DB・Git・ジャーナル・ロックを
一緒に保全したうえで、操作ID・対象依頼・期待値と実main・オブジェクト・参照を照合する。
所有プロセスがいないことを確定できないロックは動かさない。
残存物の保全・復旧はメンテナンス下の個別作業とし、本APIに汎用の解除・修復操作は設けない。
安全に復旧してロック競合が解消した後、元のメンバーが同じPOSTを再送する。
根拠が不足する場合は手動でmainを書き換えず、整合したバックアップからの復旧を検討する。

## HTTPエラー

| HTTP | 内容 |
| --- | --- |
| 401（既存処理により403の場合あり） | 未認証・無効なFirebaseトークン |
| 403 | 現在チーム外。準備後に所属を失った場合も拒否 |
| 404 | Project/Teamの不一致、依頼が存在しない・別Projectに所属する等 |
| 409 | 未承認・CLOSED、base不一致、別の未確定操作、不正な参照・記録・提出内容、復旧不能の不整合 |
| 422 | 本文への未許可フィールド等 |
| 503 | DB・Git実行・ストレージ障害、既存Gitロックとの競合 |

DB・Gitの生エラー、内部ファイルパス・秘密情報を応答に含めない。
503でも未統合と断定せずGETを確認し、同じ依頼のPOSTを再送する。
繰り返し409やロック競合になる場合は、無理な上書きをせず管理者へ連絡する。

## マイグレーションと本番反映

004_change_request_integrations.sqlは001〜003適用済みのPostgreSQLを前提とする。
新しい表、外部キーRESTRICT、NOT NULL、状態・日時・ID長のCHECK、一意制約・索引だけを追加する。
既存の依頼・レビュー・ユーザー・ProjectにUPDATE/DELETEを行わない。
再実行時に型・NULL可否・CHECKの式・外部キー・一意性・未確定部分索引を検査し、
想定外の既存構造は削除・補正せずトランザクションを中止する。

接続先とバックアップを運用者が確認し、新版の配備前に明示的に実行する例：

```sh
psql -v ON_ERROR_STOP=1 -f backend/migrations/004_change_request_integrations.sql
```

接続・認証は運用環境側で設定し、秘密をコマンド履歴やGitへ書かない。
モデルにはrequires_explicit_migrationを付け、起動時のcreate_all対象から除外した。
本作業で稼働中PostgreSQLにDDLは実行していない。別worktreeで編集し、
稼働中backendのbind mount対象のソースは変更していない。
検証は新規の専用PostgreSQL・tmpfs・専用ネットワーク・一時Gitだけで実施する。

本番配備では全Gitライターを止めて、DBとGit（ジャーナルを含む）の整合したバックアップを取る。
片方だけを過去へ復元すると復旧の根拠を失う。ロールバック時も表や操作記録を削除しない。
未移行なら統合エンドポイントは利用できない。既存一覧・レビューは新表を参照しない。

## 運用上の制約

Linuxのflock・dir_fd・O_NOFOLLOW・renameat2、ロック規約とfsyncを保証するローカル保存領域が前提。
同一Projectは提出・判定・統合も含めて直列化する。SQLiteは本番の並行制御の代用にしない。
共有Gitとジャーナルはバックエンド専用で、利用者のエディタにはマウントしない。
標準ロックを無視した直接変更、任意プロセスによる同時改ざん、NFS等での複数ホストは保証しない。
mainを外部から消した履歴がDBに存在しない場合、その事実を遡って検出することはできない。
対象のbase/headと全tree/blobを検査するが、base以前の全履歴や未参照オブジェクトの完全fsckはしない。
個人エディタの本番隔離が未実装という前段階の制約は維持する。

## テスト方法

```sh
docker build -t progress-integration-test backend
docker run --rm progress-integration-test python -m unittest discover -s tests -v
```

PostgreSQL検証は新しい一時DBへのPROJECT_TEST_DATABASE_URLを明示し、
各テストのランダムスキーマだけを操作する。既存DB・Dockerボリュームは使用しない。
Firebaseの外部通信は代替するが、認証ヘッダー・現在の所属・Project/依頼の対応は実APIを通す。
プロセス終了はLinuxのforkとos._exitでfinallyが動かない状況を一時環境に限って再現する。
WindowsではLinuxストレージ系をスキップし、Linuxコンテナで検証する。

実Firebase通信、実電源断・ストレージ障害、複数ホスト、実ブラウザ・本番エディタは未検証。

### 実行結果（2026年11月16日）

既存144件＋追加40件（統合33件、PostgreSQL移行7件）で、合計184件。

| 環境 | 成功 | スキップ |
| --- | ---: | ---: |
| Linux / PostgreSQL 15 | 183 | 1 |
| Linux / SQLite | 161 | 23 |
| Windows（既存の仮想環境） | 40 | 144 |

PostgreSQLの1件はコンテナ内にDocker CLIがないためのCompose検証。ComposeはWindowsで成功。
SQLiteは上記に加えPostgreSQL専用22件をスキップ。WindowsはLinux・PostgreSQL専用をスキップ。
正常統合、初回履歴、認証・所属再確認、状態拒否、再送、同時実行、実Gitとのロック競合、
loose/packed、CAS直前の変更、破損blob、危険なパス・DB内操作ID、DB・Git・記録の各途中失敗を検証した。
プロセスの実終了後の再送と、残存ロックを自動解除せず止まることも一時PostgreSQLで確認した。
004の実適用・再実行・旧データ保持・制約・起動時自動作成除外も成功。
Dockerビルド、git diff --checkは成功。既存TestClientの非推奨警告は出るが失敗はない。
