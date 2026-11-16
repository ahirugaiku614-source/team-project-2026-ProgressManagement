# レビュー依頼の承認・差し戻しAPI（第5段階）

## 今回追加する機能

提出済みの変更を、提出者以外のチームメンバーが一度だけ判定する。
承認は「統合してよいという判定」であり、Gitの共有mainへの統合ではない。
画面、作業領域の同期、code-server、レビュー結果の取り消し・上書きは実装しない。

処理本体は`backend/app/change_request_reviews.py`に分離した。
認証・チーム所属・Projectの対応確認は既存APIの依存関数を利用する。
モデル、API、サービス、Git参照ロックに、目的と失敗時の扱いを日本語でコメントした。

## 二つの状態とデータ構造

既存の`change_requests.state`（OPEN/CLOSED）は変更しない。
新しい`change_request_reviews`は依頼IDを主キー兼外部キーとする、一対一の判定テーブル。
子レコードがなければPENDING、あればそのdecisionを返す。
既存の依頼を更新して埋め戻す必要はなく、OPEN/CLOSEDのどちらも未判定として読める。

| state | review_decision | 承認・差し戻し |
| --- | --- | --- |
| OPEN | PENDING | 提出者以外の所属メンバーのみ可能 |
| OPEN | APPROVED | 再判定不可 |
| OPEN | CHANGES_REQUESTED | 再判定不可。修正は新しい依頼として提出 |
| CLOSED | いずれも | 判定不可 |

遷移はOPEN/PENDINGからOPEN/APPROVED、またはOPEN/CHANGES_REQUESTEDの一方向だけ。
提出のstate、コミットID、作成日時・更新日時は判定時に変更しない。
判定者IDはusersへの外部キー、判定日時はUTCのtimestamp、理由は最大2000文字。
依頼・判定者の削除はON DELETE RESTRICT。削除APIや削除運用は今回追加しない。
DB制約で、判定の二重登録、未知のdecision、必要な値のNULL、承認への理由付与を拒否する。

## API

各URLの共通部分を以下とする。

```text
/api/teams/{team_id}/projects/{project_id}/change-requests/{change_request_id}
```

全APIで`Authorization: Bearer <Firebase IDトークン>`が必要。
Firebase UIDを既存Userに解決して判定者とし、本文からユーザーID・Gitコマンド・パスは受け取らない。
承認・差し戻しの本文に未知のフィールドがあれば422。

### 承認

`POST {共通部分}/approve`

本文は不要。空オブジェクト`{}`も許可する。
成功は200で、既存のChangeRequestレスポンスに判定情報を追加して返す。

### 差し戻し

`POST {共通部分}/request-changes`

```json
{"reason":"テストで確認できるように、入力チェックを追加してください。"}
```

reasonは必須。前後の空白を除去した後に1〜2000文字で検査する。
空白のみ、不正なUnicode、改行・タブ以外の制御文字は422。
成功は200。修正後は既存の変更提出APIに新しいrequest_keyで提出する。
以前と同じ提出キーの再送は以前の依頼を返すため、再提出には使わない。

### レビュー結果

既存の一覧GETと詳細GET、および既存の提出POSTのレスポンスにも以下の5項目が加わる。
従来のid、project_id、submitted_by、title、description、state、commit、日時等は維持する。
差分APIの形式は変更しない。

未判定時の追加項目の例：

```json
{
  "review_decision": "PENDING",
  "reviewer_id": null,
  "reviewer_name": null,
  "reviewed_at": null,
  "review_reason": null
}
```

差し戻し後の追加項目の例（日時はUTC）：

```json
{
  "review_decision": "CHANGES_REQUESTED",
  "reviewer_id": 2,
  "reviewer_name": "レビュー担当",
  "reviewed_at": "2026-11-16T03:00:00",
  "review_reason": "テストで確認できるように、入力チェックを追加してください。"
}
```

APPROVEDのreview_reasonはnull。判定者名は現在のUser.nameを参照するため、表示名変更に追従する。
判定時点の表示名の履歴は保存しない。

## 処理の流れと同時操作

1. 既存の認証・所属・Project/Team対応・依頼/Project対応を確認する。
2. 既存のプロジェクト単位のファイルロックを取得する。
3. PostgreSQLではロック待ちの上限をこのトランザクションだけ5秒に設定する。
4. ProjectをFOR SHARE、所属をFOR KEY SHAREで読み直し、判定中の移動・所属削除を防ぐ。
5. 依頼行をFOR UPDATEで取得する。ORMに古い状態があっても読み直す。
6. 自己判定、CLOSED、既存の判定行を拒否する。
7. Git標準のpacked-refs.lockとbase/headの参照ロックを取得し、提出内容を検証する。
8. 判定行をINSERTしてコミットする。DBコミットまでGit参照ロックを保持する。
9. 結果を再取得して応答する。通常終了・例外のどちらでも自分が取得したロックだけを解放する。

後から来た判定は、先の確定結果を読み直して409となる。
判定テーブルの主キーも最後の二重登録防止となる。
同じ判定者が同じ内容を再送しても409とする（成功として再処理しない）。
応答が失われた場合は詳細GETで確定結果を確認する。APIは判定行をUPDATEしない。

SQLiteには同等の行ロックがない。SQLiteテストでは同一ホストのflockと主キー制約を検証する。
本運用の同時操作の保証はPostgreSQLを前提とする。

## Gitの検査とエラー

既存のDB記録と復旧用JSONの完全一致、レビュー専用base/head参照の一致を検査する。
looseとpacked-refsが共存する場合は両方を確認し、片方だけ違っていても拒否する。
packedだけがある場合はloose参照を新たに作らない。
base/headのcommit、root/中間tree、blobの内容ハッシュを照合する。
提出commitの親が保存されたbaseだけであること、通常ファイルのパス・モード・容量上限も検査する。
base以前の全履歴の完全性検査は今回の対象外。

サーバー側で確定した対象に、既存の安全な引数配列・隔離したGit設定で読み取りコマンドを実行する。
利用者のGit設定・フック・外部リモートは使用しない。
共有main、レビュー参照、Gitオブジェクト、個人作業ファイルを書き換えない。
既存のFD経由のパス検証と非通常ファイル拒否を再利用する。
ロックを無視する管理者の直接書き換えや、悪意あるプロセスの同時操作まで保証するものではない。
個人エディタの安全な本番隔離が未実装であるという前段階の制約も維持する。

| HTTP | 動作 |
| --- | --- |
| 401 | 未認証・無効なトークン（既存Firebase依存処理） |
| 403 | チーム外・自己判定 |
| 404 | 不明な依頼、Projectと依頼またはTeamの不一致等（既存依存処理を含む） |
| 409 | CLOSED/判定済み、ロック競合、DB・記録・参照・提出内容の不一致 |
| 422 | 本文の入力不備 |
| 503 | DB・Git実行・記録欠落等で安全に完了できない |

内部のパス、SQL、接続情報、Gitの生のエラーはレスポンスに含めない。
DBコミット前の失敗はロールバックし、修復後に未判定なら再試行できる。
Git参照や復旧JSONの欠落は判定APIで自動修復しない。既存の提出復旧手順・バックアップから復旧する。
DBコミット後の応答生成失敗は判定が確定している場合があるため、503でも先に詳細GETを確認する。
DB失敗の補償としてGitや既存データを削除する処理はない。

## マイグレーションと稼働中DBへの注意

`backend/migrations/003_change_request_reviews.sql`を追加した。
既存の001/002を適用済みのPostgreSQLを前提とし、新版を配備する前に運用者が明示的に適用する。
接続先とバックアップを確認し、例えば以下を実行する（秘密情報をコマンドやリポジトリに書かない）。

```sh
psql -v ON_ERROR_STOP=1 -f backend/migrations/003_change_request_reviews.sql
```

psqlの接続先は環境・認証設定で明示する。今回このコマンドを稼働中DBには実行していない。
新しいテーブルと判定者インデックスをトランザクション内で追加し、既存行にはUPDATE/DELETEしない。
同じ構造なら再実行可能。想定外の構造なら中止し、既存テーブルを削除・自動補正しない。
移行前の新版運用は不可。既存の一覧・詳細も判定テーブルを読むため、先に003を適用する。
戻す際も判定テーブルや記録は削除せず保持する。

### 今回の作業中に発生した自動DDL

稼働中バックエンドがソースをbind mountし、uvicorn --reloadで動いていることの確認が遅れた。
初期のモデル追加が再読込され、従来の起動時metadata.create_allによって、
**稼働中DBに新しい空のchange_request_reviewsテーブルが自動生成された**。
「現在起動しているPostgreSQLに適用しない」という要件を、この点で守れていない。
003 SQLを手動適用したものではないが、実DBが完全に未変更とはいえない。
確認時点の判定レコード数は0。テーブルを含め、既存データは削除せず保持している。

再発防止として、起動処理に明示移行対象のテーブルを自動作成から除外する境界を追加した。
新しい判定モデルにrequires_explicit_migrationを設定し、一時PostgreSQLで起動処理の再実行でも
003適用前にはテーブルが作られないことをテストした。
実DBの空テーブルを取り除く操作は行っていない。今後の明示的な移行・運用確認の対象とする。
マイグレーションの適用・再実行・制約検証テスト自体は、新規の一時DBだけで実施した。

## 検証方法

```sh
docker build -t progress-request-review-test backend
docker run --rm progress-request-review-test python -m unittest discover -s tests -v
```

PostgreSQLテストは専用の一時PostgreSQL 15コンテナと専用ネットワークを用意し、
PROJECT_TEST_DATABASE_URLに試験用接続先を指定する。DBデータはtmpfs、一時Gitはテスト用ディレクトリ。
既存のDB接続情報、Firebase秘密鍵、保存ボリュームはテストコンテナに渡さない。
テストごとのランダムスキーマで移行と既存行保持を確認する。
実Firebase通信だけを代替し、認証ヘッダー、所属、Projectの確認は実際のAPI処理を通す。

追加検証は、正常判定、自己判定、未認証・他チーム・別Project・不明依頼、
再判定・再送、スナップショット不変性、packedのみ・混在不整合、欠落参照、
壊れたblob、不正な親commit、古いORM状態、同時承認/差し戻し、
DB保存失敗・コミット後の応答失敗、マイグレーションの再実行・旧データ保持・各制約を含む。
PostgreSQLでは別接続が実際に行ロックで待つことと、待機後に状態を再検査することも確認する。

実Firebaseへの通信、複数ホスト・電源断、実ブラウザ、安全な本番エディタとの連携は未検証。
Windowsホストではproject_storageがfcntl・O_NOFOLLOW・dir_fdを必要とするため、
Gitストレージ系は実行せずDocker内Linuxで検証する。

### 実行結果（2026年11月16日）

既存115件に29件を追加し、合計144件。

| 環境 | 成功 | スキップ | 備考 |
| --- | ---: | ---: | --- |
| Linux / PostgreSQL 15 | 143 | 1 | コンテナ内にDocker CLIがないためCompose検証のみスキップ |
| Linux / SQLite | 130 | 14 | PostgreSQL専用13件とCompose検証をスキップ |
| Windows | 40 | 104 | Linux・PostgreSQL専用をスキップ。Compose検証は成功 |

Dockerイメージのビルド、git diff --checkも成功。
Windowsはbackendディレクトリから既存の`.venv/Scripts/python.exe`で実行した。
既定の`py -3`ではSQLAlchemy等の依存不足で実行に失敗したため、依存が揃った仮想環境で確認し直した。
既存TestClientから依存ライブラリの非推奨警告が出るが、テストは成功している。
一時PostgreSQLで003の実適用・再実行・旧OPEN/CLOSED行保持・制約・起動時自動作成除外を確認した。
自動テストは上記の一時環境だけを操作する。ただし作業中の実DB自動DDLについては前節の例外を参照。
