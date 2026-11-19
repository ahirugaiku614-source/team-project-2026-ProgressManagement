# PR #17：レビュー修正と保証の境界

最新の追加確認は[作成結果不明・期限・OS排他のレビュー](python-execution-worker-additional-review.md)に記載する。以下の530件という集計は前回修正時の記録で、今回の結果は追記欄を参照する。

## 1. 実行中の例外でも、確認済み本人の停止を試みる

従来はstart/send/readの例外をUNKNOWNへ保存するだけで、稼働中resourceに停止を試みない場合があった。今回、各実行の一時的な_Attemptに「完全IDが保存済みか」「最初の所有確認済みか」「I/O開始/閉鎖/停止を試行したか」を記録する。並行要求間でこの状態を共有しない。

例外時は実行lockを保持したまま、まずUNKNOWNを保存する。台帳の完全ID/specが一時記録と一致する場合だけdaemon/imageを再照合し、Driver.stopの最新inspectで所有者/実行ID/nonce/generation/固定設定を再確認する。確認できた本人containerだけを停止し、stream閉鎖・削除・確定404・DB完了まで揃えば枠を戻す。元の要求は503のままで、Pythonが正常終了したと偽装しない。

部分attach、start返答消失、部分send、接続断、不正frameでも同じ経路を使う。コードを再送/再実行して修復しない。最初の所有確認ができないID、作成返答不明、所有情報が変化したID、他daemonにはkill/removeしない。kill/removeを一度試して失敗した処理を、例外handlerで無条件にもう一度試すこともない。必要なら管理者recoverが別の試行として扱う。

停止確認不能でも、当該実行のlocal streamを閉じる試行は行う。これはcontainer停止の代わりではない。停止・stream閉鎖・削除が不明ならUNKNOWNと予約枠を保持する。DB書込みも失敗した場合は元の未完了予約が残り、新規実行の成功を推定しない。

## 2. 再起動時と新規受付前の照合

Service構築時にstartup_checkを自動実行し、新規受付直前にも未完了を再検査する。既定は**検出のみでEngine操作をしない**。OS lockを取得できないジョブは別管理者が処理中としてactiveへ分類し、状態を書き換えず停止もしない。取得できた未完了はUNKNOWNへ記録し、新規受付を保留する。期限だけで生存管理者のlockを奪わない。

固定のadmission lockで「未完了検査→原子的予約→実行lock取得」を直列化する。これにより予約直後・実行lock取得前の短い隙間を、追加workerが孤立と誤認しない。実行lockは通常処理/例外処理が終了するまで保持する。SQLite revisionのCASも維持する。

管理者が明示してstartup_check(recover_owned=True)を呼ぶ場合だけ、同じ排他の下で既知IDを復旧する。daemon/image・完全ID・所有情報を確認でき、停止・stream閉鎖・削除・404を確認した場合だけ完了する。ID不明/不一致/Engine障害はunresolvedとして保留。復旧APIはHTTP公開しない。通常受付時にDocker resourceを自動削除するものでもない。

旧worker版はadmission lockを使わないため、新旧版を混在させない。将来の導入では旧版を計画的に退避し、未完了を照合してから新版へ切り替える。今回は実worker・既存Dockerの停止/変更を行っていない。専用SQLiteのschema v1を維持し、自動DDLや既存PostgreSQL migrationは追加していない。

## 3. 容量管理と安全な保守

容量は無制限削除ではなく、下記の固定上限で新規受付を止める方針とした。HTTPや環境変数で上限を緩める経路はない。nonce消費・件数検査・job挿入は同じtransactionで、拒否時はnonceもrollbackする。

|観測/上限|動作|
|---|---|
|jobs 10,000件|完了tombstoneを含めて数える。上限を超える新規jobは拒否|
|nonces 100,000件|再送を含めた一回消費の証跡を保持。上限超過で受付拒否|
|実行lock 10,000件|登録済みIDだけ生成。未登録IDのlockが見つかれば拒否し、勝手に消さない|
|状態ファイル長合計512MiB|DB・journal/WAL/SHM・lockのファイル長を観測し、新規受付を保留|
|空き容量64MiB未満|未使用予約や進行中書込みのための余裕を失う前に受付を止める|

capacity_reportは件数・ファイル長・空きbytes・孤立lock数だけを返し、ファイル内容/パス/出力を公開しない。WindowsのDirEntry.statではlink数が0になる場合があったため、lock検査は直接lstatを使い、通常ファイル・単一linkを確認する。

redact_completed(before_created_ms, maximum)は管理者が明示する保守処理。作成時刻がcutoff以前でCOMPLETEDの保存結果だけをNULLにする。実行lockがまだ保持されていればそのrowをskipする。jobs row、owner、request hash、nonce、container ID、UNKNOWN/未完了の候補結果は保持する。同じ実行IDは再実行されず、秘匿後の結果再送は503となる。

nonce schema v1には安全な失効/anti-rollback情報がないためnonceの自動削除はしない。lockのunlinkは別processが保持するinodeと新規lockを分断する危険があるため行わない。証跡と排他を守る代わりに上限到達後は管理者対応が必要となる。DBを消して初期化したり、別DBへ勝手に切り替えたりしない。

NULL化はSQLite内のpage再利用や個人出力の保管削減には役立つが、物理ファイルを縮小する処理ではない。VACUUM・archive・DB rotation・tombstone移行は未実装。512MiBは受付時の目安で、進行中4件の結果/rollback journal/FS metadataによる上振れや、他processの容量消費を防ぐ物理quotaではない。実監視・保管期間・暗号化・offline保守は本番導入前の課題である。

容量上限は新規受付に適用し、既知resourceの停止/復旧を制限しない。ただしディスクが物理的に満杯ならDB完了も保証できないので、枠を解放しない。

## 4. EnginePort I/O契約

|操作|明示期限/正常終了|部分失敗・切断時|
|---|---|---|
|attach|最大1000ms。start前に非TTYのstdin/stdout/stderrが揃ったAttachAck|一部接続/期限切れは例外。startへ進まない|
|send|最大1000msと残り実行期限の小さい方。全bytes送信＋stdinだけ半closeしたSendAck|途中で送信済みでも再送しない。受信側がprefixを実行した可能性を考慮して停止試行|
|read|最大50ms、1断片最大4096 raw bytes。Noneは未到着|TCP reset、不完全多重化frame、不正stream/sizeは例外。正常EOFへ変換しない|
|eof|stdout/stderrの両方を最後までdrainしたという契約|プロセス終了と別に照合。EOFだけで成功にせず、終了済みでもEOF欠落は基盤障害|
|close|最大1000ms。当該実行のlocal接続/decoderだけを閉じてTrue。再起動後の接続なしも冪等成功|他containerの接続や管理client全体を閉じない。閉鎖不明はUNKNOWN|

出力はstdout/stderr各64KiB。超過時は上限内の部分出力だけ保持して対象を停止する。壊れたUTF-8の置換後にもbyte上限を守る。decoderも巨大なframe lengthから無制限bufferを確保してはいけない。正常な部分出力後の接続断は503で、部分結果を成功として返さない。

Serviceは返答後の期限超過を検出するが、**期限引数だけで実Docker I/Oのハングを中断できるわけではない**。実Engine adapterのsocket期限、キャンセル、kill/remove/inspect等の制御I/O期限、独立watchdog/supervisionは未実装。controllerが停止/ハング、daemon/hostが故障した場合、5秒以内の強制停止・削除は保証しない。生存processのlockをstartup scanが奪取することもない。実アダプターと独立監視を実装/検証するまで、実接続・公開実行は禁止する。

## 5. 検証・影響範囲

Windowsの開発用Python、合成Engine、一時SQLiteだけを使う。実Docker統合試験、Linux fcntl分岐、資源制限・TLS・実5秒停止はNOT_RUN。FastAPIのPOST /execute、Firebase認証、所属確認、6フィールドのAPI結果契約と既定503を維持する。

2026-11-19、Dockerを呼ぶtest_compose_config/test_editor_container_labを除外し、既存回帰も実施した。530件中369件PASS・161件skip・FAIL 0件。workerは60件（従来37＋追補23）、APIと合わせた88件がすべてPASS。追加試験は部分attach/send、入力EOF未確認、遅延返答、出力接続断、EOF不整合、stderr超過、起動時の孤立/active識別、所有不一致、容量上限、保守中のlock、nonce/tombstone保持を対象とした。

PROJECT_TEST_DATABASE_URLとEDITOR_LAB_IMAGE_IDは空にして実行。skipは主にLinux専用Git/FSと隔離PostgreSQL未指定。既存Starlette TestClientの非推奨警告は残る。実Docker統合試験と依存インストールは実施していない。

sudoers、Docker daemon/context/rootless、socket権限、既存Docker resource、Compose/volume、既存PostgreSQL/Git/workspaceは変更していない。依存インストールも行わない。rootlessは未検証のままとし、PR #17はDraftを維持してmainへマージしない。

### 追加レビュー後の最新結果（2026-11-19）

- worker：73件中70件PASS、3件SKIP、FAIL 0件。
- 既存回帰を含む全体：**543件中379件PASS、164件SKIP、FAIL 0件**（68.568秒）。
- 追加13件はWindowsで10件PASS、Linux専用3件SKIP。既存API/clientの28件もPASS。
- Dockerを呼ぶtest_compose_configとtest_editor_container_labは収集対象外。この2モジュールは上記SKIP件数に含まない。
- PROJECT_TEST_DATABASE_URLとEDITOR_LAB_IMAGE_IDを空にし、実DB・実Docker・実Firebaseへ接続していない。パッケージの追加なし。
- Windowsでは8processのSQLite予約競合、子process異常終了、msvcrt解放故障を確認。Linux fcntl専用3件と実EngineのI/O期限・5秒停止はNOT_RUN。
- Ubuntu用非Docker手順と合格条件は[追加レビュー資料](python-execution-worker-additional-review.md)を参照。Linuxで実行済みとは扱わない。
