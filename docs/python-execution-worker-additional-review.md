# PR #17 追加安全性レビュー

## 対象と実施範囲

開始時のローカルHEADとPR #17のheadは、ともに
`0273b4555dfe3811e763a9675e378bbf09233d79`。作業ブランチは
`feature/python-execution-worker`で、未コミット・未追跡ファイルはなかった。
実Engine adapterは未実装のまま。公開POST /execute、Firebase認証、Team/Project
照合、既定503、エディタ実行無効ゲートを変更していない。

## 1. create成功後に応答またはID保存を失う場合

Service._runはCREATINGを先に保存し、createの完全IDを台帳へ保存した後でだけ
所有確認・attach・startへ進む。create副作用直後の通信断では、コンテナが
存在しても完全IDが不明になる。IDを受信していても、その台帳保存が失敗すれば
同じく自動復旧に使える永続的なIDがない。

この両方を故障注入した。UNKNOWN・container_id=NULLを保持し、再送しても
再作成せず、新規要求も保留する。再起動時の検出と管理者recoverも、名前・短いID・
ラベル検索から完全IDを推測せず、kill/removeしない。元の未確定コンテナが残るので、
UNKNOWNは単なる表示上のエラーではなく**管理者照合が必要な受付停止状態**である。
この順序ではworkerがstartする前に止まるが、作成残存物がないとは保証しない。

UNKNOWN更新まで失敗した場合はCREATINGを保持する。次のService構築・受付時も
台帳検査が失敗すれば503で停止し、成功や空き枠を推定しない。予約INSERT失敗では
nonceも同じSQLiteトランザクションでrollbackされ、Engine操作へ進まない。

### 管理者が次に照合するもの

受付を停止したまま、専用台帳・lockを保持し、daemon識別子、実行ID、owner、
ownership_nonce、generation、固定image/specと、管理者が別途取得した完全IDの
実体を照合する。ラベルだけでは所有証明にならない。台帳のNULL IDへ検索結果を
自動登録したり、複数候補から選んで削除したりするツールは追加していない。
未知IDの採用・台帳修復・証跡退避の運用ツールは未実装。照合できなければ停止を
継続する。既存台帳の削除や別DBへの切替で受付を復活させてはいけない。

## 2. 実行5秒とEngine操作期限は別のもの

単調時計の実行期限は**start呼出し直前から5,000ms**。作成・準備時間を含む
要求全体の5秒SLAでも、応答・後片付けが5秒以内という保証でもない。

今回、収集中のinspect直後とread/終了確認直後に期限を再検査する。
遅延したEOF＋exit=0が届いても期限内成功へ変換しない。終了済みでもEOF欠落なら
従来どおり基盤障害503。期限超過ならtimeout・exit_code=nullとなり、確認済み本人の
停止・削除・DB確定を必要とする。遅いstartの後ではコードを送らない。

|操作|現在の参照実装|将来の実adapterに必要なもの／遅延の扱い|
|---|---|---|
|prerequisites/create|同期呼出し。数値の操作期限なし|接続・応答・全体期限。create結果不明はUNKNOWN、再作成禁止|
|attach|最大1,000msを渡し、返答後も超過を検査|socket期限・中断・部分接続の閉鎖。未確認ならstart禁止|
|start|同期呼出し、直前から実行期限を計時|有限の制御I/O期限。応答消失時も所有確認済みIDだけ停止試行|
|send|最大1,000msと実行残時間の小さい方。返答後超過検査|全量＋stdin半close確認、部分送信の再送禁止|
|read|最大50ms、断片4,096bytesを要求|実poll/decoderの期限と容量制限。遅いEOFを成功扱いしない|
|inspect|同期呼出し。数値の操作期限なし|有限の読取期限、不通を404へ変換しない。収集中は返答後再計時|
|kill|同期呼出し。数値の操作期限なし|独立した有限期限と停止確認。失敗・不明ならUNKNOWN保持|
|remove|同期呼出し。数値の操作期限なし|有限期限、停止確認と完全ID、確定404。返答消失ならUNKNOWN保持|
|close|最大1,000msを渡す|ローカルstream解放の実期限。停止の代替ではない|

**同期呼出しが戻らなければ、Serviceの単調時計による監視も進まない。**
attach/sendの予算には事前inspectによる遅延も影響する。数値引数や返答後検査は
実I/Oの期限強制・キャンセルの代わりではない。独立watchdog、制御I/Oの期限強制、
worker死亡時の実停止は未実装であり、5秒以内の停止・削除を保証しない。
これらを実証するまでは実Engine接続・学生向け公開実行を有効化してはいけない。

## 3. 4枠の排他と障害

同じ専用ローカルSQLiteのBEGIN IMMEDIATE内でUNKNOWN有無・非完了件数を検査し、
4件未満の場合だけ予約を挿入する。メモリ上のカウンターだけではない。
admission lockは「未完了検査→予約→実行lock取得」の隙間を保護し、
job lockは実行・復旧の重複操作を防ぐ。SQLite revisionによる更新競合検出も維持する。

Linuxはfcntl.flock(LOCK_EX|LOCK_NB)、Windowsはmsvcrtのbyte lockを使う。
lockファイルを削除してinodeを交換しない。今回、明示unlockが例外でもfinallyで
descriptorをcloseするよう修正した。解放失敗を成功へ読み替える処理ではない。
競合時はactiveをUNKNOWNへ変更しない。異常終了でOS lockが解放されても
未完了予約は消さず、再起動照合でUNKNOWNへ移して受付を止める。

複数process試験は8個の独立接続から4件だけ予約されることと、再接続後の件数を検査。
自分の試験子processの異常終了、SQLite triggerによる書込み失敗も検証する。
前提は同一の信頼された専用directory・同一DB・同一daemon・同じ実装版である。
別DB/別host/NFS/分散HAに対する全体4件保証ではない。

## 4. Ubuntuで次に行う非Docker検証

今回はWindows上のみで実施。以下は**手順のみで、Ubuntuでの結果はNOT_RUN**。
WSLの起動、Docker呼出し、sudo、設定変更、依存インストールは行っていない。

既存サービスと別の検証用コピーと、必要な依存がすでに入っているPython環境を使う。
本番.envやFirebase鍵、既存DBをコピーしない。必要な依存がなければ実行せず報告する。
testsは自身のTemporaryDirectoryに新規SQLiteだけを作成する。

```bash
cd /path/to/isolated-copy/backend
PROJECT_TEST_DATABASE_URL='' EDITOR_LAB_IMAGE_ID='' \
  /path/to/already-prepared-python -B -W ignore::DeprecationWarning \
  -m unittest discover -s tests -p 'test_execution_worker*.py' -v

PROJECT_TEST_DATABASE_URL='' EDITOR_LAB_IMAGE_ID='' \
  /path/to/already-prepared-python -B -W ignore::DeprecationWarning \
  -m unittest discover -s tests -p 'test_execution_api.py' -v

PROJECT_TEST_DATABASE_URL='' EDITOR_LAB_IMAGE_ID='' \
  /path/to/already-prepared-python -B -W ignore::DeprecationWarning \
  -m unittest discover -s tests -p 'test_execution_client.py' -v
```

合格条件：fcntl専用3件がSKIPされずPASS、8processの予約は4成功＋4Busy、
子process終了後はUNKNOWN・枠保持、unlock故障後も同じlockを取得できること。
Windows専用1件はUbuntuではSKIP。競合・DB故障試験も含めて予期しないFAILがあれば
実接続へ進まず停止する。Dockerを呼ぶテストモジュールを一括実行しない。

実Docker/runsc/image/資源強制/実5秒停止の検証は今回の手順に含めない。
別途承認・専用環境・監視/所有確認設計が揃うまでNOT_RUNのままにする。

## 5. 結果と保護対象

Windowsで543件中379件PASS・164件SKIP・FAIL 0件。worker単独は73件中70件PASS・
3件SKIP・FAIL 0件。Dockerを呼ぶ2モジュールは収集から除外し、この件数に含めない。
詳細はレビュー資料の最新集計を参照。追加試験はcreate応答消失、ID保存失敗、
UNKNOWN保存失敗、予約rollback、遅いEOF/inspect/start、remove応答消失からの復旧、
lock競合・解放失敗とLinux専用経路を対象とする。

変更対象はworker内の期限判定・lock解放・契約コメント、合成テスト、資料のみ。
Firebase・公開API実装、個人エディタ、KVM、共有Git、workspace、PostgreSQL、
Docker Compose/volume、sudoers/socket/daemon設定を変更していない。
実Docker・実VM・実Firebase・クラウドへの接続も行わない。
