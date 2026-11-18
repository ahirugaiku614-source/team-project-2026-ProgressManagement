# 静的・モック試験結果と将来の実機手順

## 今回の結果

環境はWindows PowerShell＋既存開発用Python 3.14。既存Docker Engine・PostgreSQL・保存領域に接続せず、新しいworktreeで試験した。PR #14マージ済みmain `54eb8d0`を基点とする。

|試験|結果|
|---|---|
|新規`test_gvisor_lab.py`|30件成功|
|既存を含む全体回帰|407件中245件成功・162件skip（既存377件は215成功・162skip、新規30成功）|
|gVisor静的checker|`STATIC_CONSISTENT_ONLY`、実行・配備・接続false|
|既存実行ゲート3ファイルの保護hash|一致、変更なし|
|イメージbuild、コンテナ起動、実runsc、実ブラウザ|NOT_RUN|
|実Linux隔離・quota・packet遮断・実Firebase/HTTPS|NOT_RUN|

新規30件には複数のsubTestを含む。複数プロセス試験は、8つの独立Pythonプロセスから同じ一時SQLiteへ予約し、4件だけを許可した。同一User×Projectへの6並列予約は1件だけ許可した。これはSQLiteの予約排他の試験であり、8台のコンテナを起動したものではない。

|確認事項|今回の証拠|実機での状態|
|---|---|---|
|runsc不在・runcフォールバック拒否|Capabilities/設定の不正値と既定executorの拒否|NOT_RUN|
|root/privileged/capability/未知mount拒否|canonical設定の改変拒否|NOT_RUN|
|socket・鍵・他者領域・path traversal拒否|任意sourceへの改変拒否。実パスresolverは未実装|NOT_RUN|
|DB/管理網/Git/metadata/他sandbox通信拒否|network none以外の設定拒否＋全拒否モック|NOT_RUN：実packetを送っていない|
|CPU/RAM/pids/disk上限|設定検査、予約量超過と容量証跡不足の拒否|NOT_RUN：実強制を未検証|
|所有者・別Project・host principal|異なるID、UID衝突、owner不一致の拒否|実UID mappingはNOT_RUN|
|世代・lease・操作再送|改変ticket/二重dispatch拒否、履歴と現在状態の区別|実境界fencingはNOT_RUN|
|停止・再起動のdata保持|合成data保持、停止中もdisk枠を保持|実volumeはNOT_RUN|
|保存失敗/実行中job/未保存|停止前拒否、最終境界失敗時の隔離|実client保存はNOT_RUN|
|応答消失/途中終了/DB失敗|UNKNOWN/隔離、予約維持、台帳再open|実worker再起動はNOT_RUN|
|所属取消し|取消し後のstate/finish拒否|Firebase・実Team DBはNOT_RUN|
|秘密情報とレシピ改変|固定例外/未知CLI引数の非反射、hash・manifest検査|実image/log収集はNOT_RUN|
|既存API・安全ゲート|既存回帰試験＋保護hash検査|実本番接続は行わない|

スキップは成功件数に含めない。実際に実行していない試験をPASSへ置き換えない。

全体回帰は`PROJECT_TEST_DATABASE_URL`と`EDITOR_LAB_IMAGE_ID`を空にして`python -W ignore::DeprecationWarning -m unittest discover -s tests -q`を実行した。162件skipは主にLinux専用のGit/ファイルシステム保護、隔離PostgreSQL未指定、明示opt-inの実コンテナ試験による。現在稼働中のDBを代用しない。既存Composeの構文試験は偽の試験設定を使う`docker compose config`のみで、Engineへ接続・作成・再起動はしない。既存Starlette TestClientの非推奨警告は残る。

## 将来の専用ホストでの前提確認

以下は今後の実施計画で、今回実施する指示ではない。別途承認された専用検証host・試験アカウント・一時データ・費用上限が準備できるまで進まない。既存Windows Docker Desktop、既存DB、Git、workspace、volumeを代用しない。

1. 管理者がhost識別子、試験責任者、実施範囲、予算、接続経路を記録する。アプリhostと分離し、本番鍵・DB経路・共有Git経路がないことを確認する。
2. Linux architecture/kernel/systemd、user namespace可否、subuid/subgid範囲、user別rootless daemonの権限を確認する。runsc/caller/runtimeの全process chainを調べ、rootful daemonやdockerグループ経由なら不合格。単なる登録名や`info`表示を証拠にしない。
3. 使用release・binary SHA256・署名検証方法を固定し、runsc＋systrapを明示する。非root UID1000 mapping、先行network namespace、seccomp/LSMの整合を検査する。組込みrootlessの制約を別フラグで隠さない。
4. delegated cgroup v2のcpu/memory/pids controllerとswap上限、本人root/data quota、inode上限を実際に読取り検査する。制限を設定できない場合は停止する。全rootless環境のsystemd起動方法などの特権設定は別の導入レビューで決める。
5. 機密を含まない専用volumeを各User×Projectへ新規割当てする。既存volumeを移動/chownしない。host principalとsubuidの衝突、symlink/hardlink/device、親directoryの権限、path FD検証が未解決なら停止する。
6. 公式code-serverのレビュー済みdigest、各architectureのpackage、SBOM・脆弱性を検査してから、専用contextだけでbuildする。DockerfileのFROM制約は文字列だけで真正性を保証しない。今回のmanifestのnullを架空digestへ置換しない。
7. 現entrypointは常時拒否のまま。実エディタを起動するには、署名済み証跡・実executor・保存集約器・接続経路・下記試験を実装する**別PRが必要**。環境変数やCLIで今回のゲートを解除する経路はない。

Windows利用者はまず学校担当者に専用Linux検証環境の準備を依頼し、host名と責任範囲を確認する。既存Docker設定を書き換えたり、自分のPCへ自動インストールする手順は含めない。今回実行できるPowerShell操作はREADMEの静的checker/単体テストまでである。

## 実機1人→2人の合格基準案

実施時は各行へrun_id・host/build/runtime digest・時刻・匿名化結果・判定を保存する。セッション、credential、本人編集内容をログへ残さない。

|段階|測定・攻撃試験|合格基準案／停止条件|
|---|---|---|
|起動前|runsc無効/不在、runc要求、未知runtime、制限不足|すべて起動拒否。代替runtimeで起動したら中止|
|1人|Python/GCC/Node/Git local利用、code-server/LSP、保存再起動|data checksum一致、toolchain正常、OOMなし。性能はp50/p95/peakRSS/CPUを測定し、学校で許容する基準を別決定|
|1人・資源|CPU負荷、メモリ増加、process増加、容量/inode上限|hostの予約余裕を侵食しない。data消失なし。負荷試験は専用上限内のみで、host障害を狙わない|
|1人・ファイル|本人volume外、socket、host鍵、共有Git、別Project、symlink/hardlink|読取り・書込み不可。1件でも到達可能なら隔離して中止|
|1人・ネットワーク|許可された合成先で管理網/DB/Git/metadata相当の遮断、IPv4/IPv6/loopback/Unix socket|許可リレー以外へ通信不可。実provider metadataへ無断でprobeしない|
|2人|別principal/別volume/別Project、相互通信、同時負荷|相互アクセス不可、予約内、世代混線なし。CPU余裕不足なら2台目拒否を合格とし、2人実用可能とはしない|
|接続|将来の独立HTTPS、Firebase失効/所属取消し、他者URL、Host/Origin、WebSocket|本人だけ接続、15秒以内の再認可、認証障害は拒否。VMRoute偽装は禁止|
|障害|起動途中停止、応答消失、保存失敗、DB unavailable、古いfence|UNKNOWNの枠維持、再作成なし、本人data保持、監査可能|
|backup|暗号化した外部backupから新しい検証volumeへ復元|checksum・owner・generation一致。復元先に既存領域を指定しない|

全項目合格までは外部公開せず、実行無効ゲートを維持する。network noneのままブラウザが開けないことを理由に、host networkや認証なし公開へ変更してはいけない。

## 作成途中の失敗と後片付け

将来の実workerは、作成要求前にrun_id/operation_id、owner、generation、設定hash、予定volume/sandbox IDを専用journalへ保存する。応答を失ったら、ラベルだけで削除対象を列挙せず、**記録したimmutable resource ID・所有者・generation・volume IDを読取り照合**する。別実施のresource、名前だけ一致、所有者不明、journal不在は触らず隔離/管理者確認とする。

停止失敗時は強制終了・枠解放・再作成・data削除をしない。作成済みか不明なresourceは残存候補としてIDと状態だけ報告する。今回のコードには実作成・停止・削除クライアントがないため、この識別は設計のみ。

後片付けも別途承認された検証resourceに限る。最初に保存/backup/停止の確認を行い、本人データの保持と診断用実行resourceの除去を別扱いにする。正確なIDの確認と正常停止が完了した診断resourceだけを管理者が通常手段で処理し、不明なら残す。大量削除、prune、down -v、強制停止、既存volume削除、共有Git変更の手順は含めない。

今回の自動試験の後片付けは`TemporaryDirectory`配下の合成SQLiteと、自分でspawnしたPython子プロセスに限定する。イメージ、コンテナ、volume、VMは作成していない。
