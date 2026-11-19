# Python実行worker：Draft実装・保証範囲

## 今回の到達点

FastAPIとは別processへ配備するための`backend/execution_worker/`を追加した。予約・署名検証・固定コンテナ設定・出力監視・停止/削除確認・復旧処理は実装し、**合成Engineで試験した**。実Docker SDK/CLI/socketアダプター、API→workerの実通信、TLS/mTLS peer検証、秘密の配備、確認済みPython imageは未実装/未準備である。

したがって本番FastAPIの`POST /execute`は、認証/所属確認後も既定503を維持する。workerの既定ASGI appも503で、`python -B -m execution_worker`は未接続JSONを出力して終了値2となり、サーバー・Engineを起動しない。環境変数で実行を有効化する分岐はない。これは実機実行可能な完成品ではなく、Draftでレビューする実行制御実装である。

Ubuntuのrootful Docker＋runscで2コンテナ・ファイル分離・基本設定を検証できたという利用者の報告は前提情報とする。今回それを再実測せず、4件並行・128MiB・出力過多・異常終了後の停止はNOT_RUN。rootlessは未検証で、rootfulと混同しない。既存KVM/個人エディタの実装は維持する。

## ファイルと役割

|ファイル|役割|
|---|---|
|`execution_worker/contracts.py`|固定例外、完全ID、JSON fingerprint、inspect/frame契約|
|`execution_worker/auth.py`|peer必須・HMAC-SHA256署名・5秒有効・重複JSON拒否|
|`execution_worker/registry.py`|専用SQLiteによる4枠、nonce一回消費、CAS revision、実行別OS lock|
|`execution_worker/engine.py`|固定Docker create設定、完全ID/spec照合、停止/削除後の確認|
|`execution_worker/service.py`|受付、意図保存、起動、出力監視、結果保存、後片付け、管理者復旧|
|`execution_worker/api.py`|独立ASGIの限定内部API。既定未接続、peerをheaderから信用しない|
|`execution_worker/__main__.py`|実接続を開始しない既定CLI|
|`tests/execution_worker_fakes.py`|実コード評価なしの合成Engine・時計・署名・子process|
|`tests/test_execution_worker.py`|正常/異常/並行/認証/復旧の試験|

前回7ファイルは別commitで先に保存した。worker追加に伴う既存コード変更は`execution_client.py`のworker満枠→ExecutionBusy→HTTP 429伝播と資料の更新が中心。API本文・結果の6フィールド、Firebase失効確認・現在所属・Project対応確認を維持する。新しいDBモデルやPostgreSQL migrationは追加しない。

## 認証とAPI境界

ブラウザ→FastAPIのFirebase認証は従来どおり。workerはFirebase秘密鍵を受け取らず、**認証されたAPIがUser/Team/Projectを確認したという署名済みの申告を信頼する境界**である。学生へworker署名keyを渡さない。

内部は`POST /internal/execute`だけで、RUN_PYTHONの既存ExecutionCommandを受け付け、ExecutionReplyを返す。任意exec・host path・image・URL・削除・復旧APIは公開しない。image/daemonはworker設定で固定する。

将来の通信は認証された専用経路に限定する。実TLS/mTLS検証器は未実装で、`create_app()`の既定peer verifierは未構成。テストは合成peer検証器を明示注入する。`x-peer-verified: true`等のHTTP headerだけでは許可しない。HMAC単独で安全なネットワーク配備が完成したとは扱わない。

署名形式は`v1\n<timestamp_ms>\n<nonce UUID>\n<SHA256(raw body)>`へのHMAC-SHA256。headerは`x-execution-timestamp`、`x-execution-nonce`、`x-execution-signature`。5秒以内の過去時刻を要求し、未来時刻・時計逆行・不正署名・重複JSON key・NaN等を拒否する。DB待機/準備中にも失効を再検査する。

nonceは予約transaction内で永続一回消費し、process再起動後も再利用できない。内部body全体を512KiBに制限し、JSON escaping後の本文とは別にcodeのUTF-8 64KiB上限を再検査する。key取得/rotation/mTLS失効/peer ID割当は未実装なので、実接続は503のまま。key・source・raw例外を応答/ログへ反射しない。

## 固定コンテナ設定

`specification()`はDocker create相当の固定設定を作る。EnginePortは実APIへの変換境界であり、実装済みのネットワーククライアントではない。検証済みlocal image ID（sha256）とdaemon IDを台帳へ固定し、tag変更・別daemon/imageへの乗換えを拒否する。pull/buildの操作は持たない。

|設定|値|
|---|---|
|runtime/network|runsc必須、none、port非公開。runcへのfallbackなし|
|実行User|1000:1000、privileged=false、capability ALL削除、no-new-privileges|
|root/host mount|read-only、bind/mount/deviceなし。workspace・鍵・Docker socketを渡さない|
|CPU/RAM|NanoCpus=500000000、Memory=134217728bytes|
|swap|MemorySwap=Memoryで追加swapを許可しない（実強制はNOT_RUN）|
|pids/tmpfs/shm|pids32、/tmp16MiB、/work16MiB、shm8MiB。tmpfsはnoexec/nosuid/nodev|
|実行入口|固定Python3の`-I -B -u -`へstdinでcodeを送りEOF。hostで評価しない|
|TTY/log/削除|TTY=false、log driver none、AutoRemove=false、restartなし|

imageのVOLUME宣言、継承ENVに秘密情報がないこと、architecture・Pythonの互換性も確認必須。createでEnvを指定してもimageの他ENVが消えるとは限らないため、image metadataの審査を省略しない。実Engineアダプターはinspectを安全性に関係する全設定へ正規化し、危険な未知fieldを黙って落としてはいけない。

`prerequisites()`は実hostで確認すべき契約で、現在返すのはモックだけ。runtime登録名だけを実行証明にせず、runsc/全資源制限・image・daemonを実証できなければ拒否する。CPU/RAM/tmpfs等の数値は設定案であり、性能・隔離の実測結果ではない。

## 4枠、同時操作、再送

同一host/daemonの全workerは**同じ専用local SQLite**を使う。BEGIN IMMEDIATEでnonce消費・同一execution_id照合・全体4枠予約を一括確定する。process内counterは上限の根拠ではない。別DBのworker群、NFS、複数host、HAは対象外で、中央予約機関の別設計が必要。

RESERVED/CREATING/RUNNING/CLEANING/UNKNOWNは枠を使用し、COMPLETEDだけ解放する。UNKNOWNが1件でもあれば、新規受付を保留する。署名付き同一実行の再送は新しいnonceを使い、同じowner/source/policyの完了結果だけ返す。active/UNKNOWNは再実行せず503。ブラウザが新しいexecution_idで再POSTする重複実行は別問題で、HTTP Idempotency-Keyは未実装。

実行単位のOS lockで通常処理と管理者復旧を排他し、SQL revisionのCASで古い更新を拒否する。generationは実行ごとに新規IDを作る方式で1に固定し、同じIDで新世代を作らない。Dockerはfencing tokenを検査しないため、DB revisionだけで権限ある別管理者を制御できるとは主張しない。lockを強制削除せず、満枠の拒否要求ごとに新しいlockを作らない。

## 出力・5秒期限・結果互換性

start前にattachし、start要求直前から単調時計で5秒を計測する。stdout/stderrは最大4096 raw bytesの非TTY frameで交互に回収し、各65536bytes以内だけ保持する。EOFと停止の両方を確認して終了コードを確定する。壊れたUTF-8は置換し、置換後のbytesも上限へ収める。

期限到達/出力超過は所有確認後に対象全体を停止し、停止確認後だけ削除する。正常・Python例外は確認できたexit code、timeoutは既存契約どおりnull。出力超過はstatus=error、exit_code=null、上限内の部分出力と固定理由を返す。ポリシー違反時の終了コードは公開せず、成功0へ偽装しない。公開statusの3種類を増やさない。

実RPCには短いI/O期限と非blocking readが必要で、EnginePort実装の契約となる。今回それを実装・実測していない。daemon停止、RPCハング、worker死亡、host障害時に5秒以内の停止/削除を保証するものではない。特にstream障害時はUNKNOWNへ保留し、明示復旧まで実resourceが残る可能性がある。独立監視・supervision・実I/O期限を確立するまでは公開実行禁止。

## 所有確認・障害復旧

作成前にowner/request hash/spec/ownership nonceを保存し、作成返答の完全64桁IDを保存してからstartする。全停止/削除前にIDとspec全体（owner、実行ID、nonce、generation、image、runtime等）を照合する。名前/label一致だけで採用しない。他containerに対する操作、prune、一括削除、volume削除、force removeはない。

|障害|保持する状態・復旧|
|---|---|
|create前の前提不足/DB失敗|実resourceなし。予約前なら何もしない。RESERVEDなら完了失敗として解放可能|
|create返答消失・完全ID未保存|UNKNOWN、枠保持。名前やlabelで推測削除せず管理者確認|
|start/stream失敗|既知IDを保持してUNKNOWN。管理者のrecoverで所有確認から再開|
|kill失敗/停止未確認|UNKNOWN。removeへ進まない|
|remove失敗/確定404未確認|UNKNOWN。枠を戻さない|
|remove成功後のDB失敗|削除前に保存した候補結果を保持。再実行せず、既知IDの確定404とDB復旧を確認|
|process死亡|OS lockは解放されるが予約は残る。CREATING以降は不明を空き枠とみなさない|
|active処理中のrecover要求|lock取得失敗。稼働中の状態を書き換えない|

recoverは管理者用の内部メソッドだけでHTTP公開しない。RESERVEDはcreate前と確定できる。既知IDは同じdaemon/imageでinspect→必要なら停止→削除→確定404→DB完了。未知IDや所有不一致は触らない。最終result不明の完了失敗は再送でも503となり、正常実行へ偽装しない。

台帳は通常ファイル/単一link/専用識別子を検査し、既存/途中ファイルを消さない。管理者だけが操作できるprivate local directoryが前提で、敵対的な親directory差替えやroot侵害を防ぐものではない。Windows ACL・Linux本番権限の実機検証は未実施。codeは保存せずhashで照合するが、候補結果にはstdout/stderrが含まれる。保存容量、保管期間、backup/暗号化/rotationは本番導入前の別課題。

## テストと未検証

2026-11-19、Windowsの開発用Python 3.14で検証した。新規worker37件と既存実行API28件を含む回帰は507件中346件PASS・161件skip・FAIL 0件。正常・例外・exit code・期限・出力過多・認証・4枠・別API client・8process予約・所有不一致・作成返答消失・kill/remove失敗・DB失敗・process異常終了・復旧をモックで確認した。

PROJECT_TEST_DATABASE_URLとEDITOR_LAB_IMAGE_IDは空にし、Docker関連2moduleを除いたunittestを実行した。skipは主にWindowsでは実行できないLinux Git/FS試験と未指定の隔離PostgreSQL試験。既存Starlette TestClientの非推奨警告は残る。実行別lockのWindows分岐は試験済みだが、Linux fcntl分岐の実機試験はNOT_RUN。依存追加インストールはしていない。

実Dockerを呼ぶ`test_compose_config`と`test_editor_container_lab`は除外する。実Engine/gVisor/image、Linux cgroup/namespace、実CPU/RAM/swap/pids/tmpfs/shm、TLS、実4コンテナ・実5秒停止、実ネットワーク遮断はすべてNOT_RUN。

既存Docker Desktop/Ubuntu daemon/default context/rootless/sudoers/socket権限、Compose/volume、PostgreSQL/DDL、共有Git/workspace、Firebase/KVM/個人エディタは変更していない。rootful Docker制御は強い管理権限であり、別process化だけでは権限問題を解決しない。権限付与やsudo自動化を追加せず、確認済みの専用実行主体・相互認証・実Engineアダプターが整うまでは既定503を維持する。
