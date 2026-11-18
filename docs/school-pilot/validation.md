# 実VM検証環境と安全性・性能試験計画

KVMの以下の条件は維持する。他の方式を比較する場合の追加停止条件は
[追加比較の安全性試験](isolation-cloud-comparison.md#安全性試験の追加条件全件not_run)を併用する。
方式を替えるだけでKVM向け合格証跡を流用しない。全方式の実機試験はNOT_RUNのまま。

## 今回行うこと／行わないこと

今回は静的な計算・テストだけを実行する。以下の負荷・実機試験は**すべてNOT_RUN**である。
設計書の目標値は合格条件案であり、達成済み性能やSLAではない。
既存Docker Desktop、PostgreSQL、共有Git、workspace、ボリュームは試験対象にしない。
実VM、クラウド契約、課金、認証変更、外部公開、実行ゲート解除も行わない。

## 将来必要な隔離した試験環境

| 区分 | 条件 |
| --- | --- |
| アプリ試験系 | 新規の専用Web/APIと一時PostgreSQL、新規Git保存・合成40ユーザー/5チーム×8人/Project。既存DB dump・生徒情報は使わない |
| 負荷生成 | 試験サーバーとは別のマシン。時刻同期、制御した同時開始、CPU/回線余力を計測。既存URLを誤指定できないallowlistと開始前の環境ID確認 |
| VM試験系 | 既存環境とは別の学校承認Linux実機/KVM。A32GiBとB64GiBを別予算で比較。新規2TiB保存、新規外部4TiB、合成ファイルだけ |
| 管理/接続系 | 専用managerと限定authorizer、管理鍵を持たないgateway。試験用独立HTTPS/DNS/TLS、VM guest TLS identityと外側ACLを別途準備・承認 |
| 認証 | 最初はmockで純粋なAPI性能を測る。その後、別途承認した試験Firebase project/合成アカウントで40ログインと失効を確認。実利用者のtoken/keyは使わない |
| 監視 | API・認可・DB pool/lock・Git実行・host CPU/RAM/IO・disk/inode/thin metadata・network/backupを採取。token/Cookie/コード/verifier/本文をログに記録しない |

準備・安全確認が完了するまで以下の試験を開始しない。実基盤に必要なコマンドは今回の成果物へ自動化しない。
将来PowerShell利用者は、別フォルダーに試験IDと台帳を用意→学校の管理者が対象実機/ネットワークを確認→
合成データだけで1VM→2VM→4VMの順に承認して実施する。
詳細の構築・復旧は[第8段階runbook](../editor-foundation/runbook.md)を継承し、
既存Dockerの停止、既存volume移送、既存workspaceのchown等を手順に含めない。

## 測定共通ルール

- A/Bを同じデータ・アプリrevision・負荷生成機・cache条件で比較する。warmup5分、定常30分、
  3回反復し、cold startを別測定。主要HTTP群は各1,000件以上を目安に不足なら延長する。
- p50/p95/p99、throughput、エラー率、timeout、CPU/RAM/swap、IO待ち、DB接続数/lock待ちを同時に報告。
  成功時だけを集計せず、失敗・拒否も別件数にする。期待403/409等を正常200へ混ぜない。
- 基準値は学校管理者の合意前の案。超えたら原因を調査してA→B等を比較し、基準を後から都合よく緩めない。
  不整合/権限漏れ/データ損失は性能が速くても不合格。
- リクエストごとの合成操作IDと期待結果を別台帳に保持し、最終DB/Git状態と照合する。
  タスク更新を違う対象へ分散する試験と、同じ対象への競合試験は分ける。
  同一タスクの古い更新上書き拒否は現在のAPIで未保証の可能性があり、試験で判明したら別課題とし、本PRで変更しない。

## 性能・動作試験一覧（全件未実施）

| 項目 | 測定方法 | 合格基準案 | 必要環境 |
| --- | --- | --- | --- |
| 40人同時ログイン | 同時開始を1秒以内に揃え、認証→me/Team取得まで計測。mockと承認済み試験Firebaseを別報告 | API側p95≤2秒、全40完了≤5秒、予期しない失敗0。Firebase実ネットワーク遅延は内訳化 | アプリ試験系、負荷機、後段は専用試験Firebase |
| 40人の進捗・タスク更新 | 40セッション、各5秒にread/20秒にwrite（約8read+2write req/s）、30分。固有更新と同一対象競合を分離 | 通常read/write p95≤500ms、p99≤1.5秒、非期待5xx/timeout<1%、欠落/他人更新0 | 一時DB・40合成ユーザー |
| 複数チーム同時操作 | 5チーム×8人でTeam/Project/Task更新、他チームID差替えを並行 | 同条件のp95増加20%以内、他チーム拒否100%、Team/Project取り違え0 | 複数Team/Projectを持つ一時DB |
| Gitレビュー同時閲覧 | 40人が各自Teamの一覧・詳細・100KiB patchを5秒間隔で閲覧。上限付近/超過256KiB・バイナリも分離 | 一覧p95≤500ms、差分p95≤2秒、サイズ上限維持、Git main/参照不変、CPU/IO飽和なし | 新規bare/変更依頼・隔離Linuxストレージ |
| 提出/承認/統合との混在 | 閲覧中に別メンバーが提出・判定、base一致/不一致の統合を行い履歴・snapshot・journalを検証 | 誤承認/二重統合/更新欠落0、期待競合は409等で安全に停止 | 一時PostgreSQLと専用Git。既存移行履歴を使わない |
| 4 VM同時起動 | cold image cacheとwarmを分け、4組を同時要求。予約確定→guest ready/TLS/ACL確認まで | warm p95≤120秒、cold≤180秒を初期案。4組の所有関係一致100%、swap依存なし、未知ready0 | 別KVM実機、新規disk、承認済み試験gateway |
| 5台目の拒否 | 4予約がRUNNING/PREPARING/STOPPING/QUARANTINEDの各場合に5組目を要求。別クライアント/プロセスを併用 | 拒否≤2秒、枠≤4、追加VM/disk作成0。未知状態を空き扱いしない | 同上。現行参照実装のmock試験も維持 |
| 停止/再起動/保存 | 各組に合成ファイルを書きhash記録→正常停止→再起動を10回、重複要求・応答消失を注入 | ファイルhash一致100%、他User/Project変更0、同disk二重RW0、不明は隔離、削除0 | 新規VM/disk・専用台帳 |
| 容量監視・不足停止 | 小容量の専用試験poolに合成データを増やし75/80/85%を通過。thin metadata/inode・IO errorも独立注入 | 監視更新≤5秒、閾値で新規予約拒否100%、停止中も予約保持。4 VM急増時も余裕を使い切らず、データ損失0 | 捨ててもよい新規pool。既存diskを埋めない |
| WebとVM負荷の分離 | 40人API+レビュー負荷と4 VMビルド/編集を同時実行、VM無しの結果と比較 | API p95増加20%以内かつ上記SLO維持、ゲストからDB/Git/管理系への到達0 | 分離A/B、別負荷機、外側ACL |
| 将来10/20台 | 専用容量モデル→mock分散競合→承認後の新規host群で段階測定。初期4枠では実施しない | 承認済み別quotaが全体で保持、host別CPU/RAM/diskとN+1予算、API基準/隔離/復旧を維持 | 別設計の権威台帳・fencing、10台は最低2/推奨余裕3host、20台は最低4/余裕5host |

80%を超えてから手動で気づく方式は合格にしない。5秒に増え得る最悪byte数と正常停止に必要な余裕を見積り、
ゲスト外quota/IO制御と予約で保全する。実測で間に合わなければ閾値を早める設計を再承認する。
API40人の合格とVM4台の合格は別々に記録し、一方だけで全体合格としない。

## 安全性・復旧試験一覧（全件未実施）

| 試験 | 方法 | 合格/停止条件案 |
| --- | --- | --- |
| 任意コードからの境界確認 | 各VMターミナルから他VM/別Project/共有Git/DB/manager/libvirt/Docker socket/metadataへアクセス試行 | ゲスト外で全拒否。秘密/他disk/管理socket発見で即不合格 |
| HTTPSと権限失効 | 合成本人/他人/取消済み所属、Host/SNI/Origin偽装、コード再送、HTTPとidle/連続WS両方向で照会 | 本人だけ接続、全偽装拒否、現在所属・token失効/世代変更で最大15秒以内に実ソケットを閉鎖 |
| 認可サービス障害 | 認可RPC遅延/無応答とgateway timerの独立動作を試験系だけで注入 | 新規拒否、継続を最後の許可deadline内で遮断。RPC待ちで切断が遅れたら停止 |
| 管理/host障害 | 合成処理の準備/起動成功応答前後で専用試験プロセスを止め、journal・台帳・host状態照合 | 新規VMを重ねない、枠保持、fencing後にのみ再開。SQLiteコピーで復旧しない |
| 外部backup復元 | 暗号鍵とmanifest、40環境の整合点を別の新規領域へ復元。元データを上書きしない | 全hash/所有者一致、世代巻戻しなし、元VMと同時RWなし、失効session再利用不可 |
| RPO/RTO | DB/Gitと編集diskを別測定。目標RPO24h、Web/API RTO4h、4人分VM RTO8h、40環境復元24hを暫定案 | 鍵・台帳を含めて達成しなければ不合格。授業/災害時の目標を学校と合意 |
| 分散/ネットワーク分断 | 将来のmock・専用hostでleader分断、遅延命令、予約競合、stale generationを注入 | 同User×Project二重配置0、全体quota超過0。不明hostの枠を解放しない |
| 回帰/情報漏えい | 既存APIの回帰、アクセスログ/例外/監視の出力を合成秘密値で確認 | 既存レビュー/統合の破壊0、token/Cookie/コード/verifier/生徒情報の記録0 |

全40環境主保存1584GiBの転送は、理想値でも1Gbit/sで約3.8時間、100Mbit/sで約37.8時間。
暗号化・IO・小ファイル・検証時間を加えるため、100Mbit/sで全体24h復元は成立しない。
外部完全backup3040GiBは100Mbit/sで約72.5時間相当となる。
回線不足なら学校承認の暗号化媒体の別場所保管・回転、backup方式、RPO/RTOを再設計する。
週1回しか別場所へ運べない場合、校舎全体災害のRPOは最大7日となり24hを満たさない。
ミラーや同じ校舎のコピーを別障害領域の代わりにしない。

## 開始/停止の判断

開始条件：学校の情報管理/予算承認、専用試験機・合成データ・別保存領域・別Firebase/HTTPS、
監視・当番・バックアップ鍵・復元先が準備済みであること。現時点では未準備。

停止条件：既存環境に向く設定/経路、他人の領域/秘密の参照、quota超過、未知状態の自動復旧、
データ破損、復元不能、15秒切断不達、認可照会失敗時の許可、監視不能、容量閾値超過。
試験対象だけの新規受付を止め記録を保全する。既存環境へのstop/down/prune、
実行ゲート解除、disk削除・強制停止による復旧は行わない。

合格証跡は測定日時・revision・合成環境ID・値・失敗件数・担当者の確認を残す。
JSONにPASSと書くだけで配備許可にはしない。学校試験への導入も別PR/承認の対象。

## 今回の静的検証

別worktreeのPowerShellで実行する。Python環境は開発venvを指定できる。

```powershell
Set-Location <別worktree>\backend
python -m tools.school_pilot_check
python -m unittest discover -s tests -p test_school_pilot.py -v
python tools/editor_foundation_check.py
# 既存回帰も実DB・実コンテナを無効にしてモック/一時データで実施
$env:PROJECT_TEST_DATABASE_URL = ''
$env:EDITOR_LAB_IMAGE_ID = ''
python -m unittest discover -s tests -v
```

静的計算で確認するもの：40/80環境、1/2TiB不足/成立、外部backup、20%空きの境界、
32/64GiBのCPU/RAM予算、全体4枠維持、10/20台とN+1、数値/JSON検証、保護ソース不変、
実行/配備/認証変更/性能検証の全許可false。
静的16件すべて成功。全回帰347件は成功185件・スキップ162件・失敗0件だった。
新checkerの計画計算と既存checkerの保護ハッシュは合格し、許可値はすべてfalseのまま。
内訳は[入口文書](README.md)を参照する。上記性能/実機試験のNOT_RUNを変更するものではない。
