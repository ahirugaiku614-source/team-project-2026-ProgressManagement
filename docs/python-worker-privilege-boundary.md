# Python実行ワーカーの権限分離設計（PR #18向け）

## 到達点と前提

最新main `f7f8b10d8736d15d4590b434d87c0fea82acb115`はPR #17をマージ済み。
開始時の作業ツリーはcleanで、`feature/python-worker-privilege-boundary`を作成した。
今回は設計書と副作用のない静的検査だけを追加する。実Docker adapter、daemon設定、
通信、インストール、コンテナ操作は追加・実行しない。既存コードは変更しない。

推奨するのは**アプリと別の専用Linux検証ホストに、実行専用OSユーザーのrootless
Docker daemonと、RUN_PYTHONだけを受け付ける限定操作ブローカーを配置する方式**。
既存Docker Desktopや利用者が検証したrootful default daemonを転用しない。
rootless＋runscの成立・性能・制限強制は未確認なので、採用確定や実行許可ではなく、
次に検証する第一候補である。条件を満たせなければ停止し、rootful/runcへ切り替えない。

## 方式の比較

|方式|権限範囲・攻撃経路|既存環境への影響|難易度・運用課題|判断|
|---|---|---|---|---|
|rootless＋専用daemon＋限定ブローカー|daemon権限を専用OSユーザーへ限定。ブローカー侵害で同UIDのファイル・全実行環境が危険。kernel/user namespace/runsc脆弱性は残る|新規の別検証ホストだけで準備。既存daemon/contextは触らない|中〜高。user namespace、subuid/subgid、cgroup v2/systemd委譲、runsc・image互換性の実証が必要|第一候補。hostのroot権限を常用しない利点があるが、rootlessを検証済みとはしない|
|rootful＋限定ブローカー|root daemonを操作する部分はhost root相当の信頼境界。固定操作でも実装欠陥・daemon侵害によりhost全体が危険|アプリ・DB・Gitを持たない専用hostと専用daemonが必須。既存rootful daemonを共有しない|高。特権部の最小化、IPC認証、host再構築、監査、専用ネットワークの運用が必要|rootlessで必要な制限を実証できない場合の再検討候補。別process化だけで安全としない|
|Docker認可plugin／厳格なAPI認可|daemonへの要求をdefault deny。ただしdaemon権限そのものは縮小しない。URI正規化・未知field・hijack・認可状態の競合が攻撃経路|既存daemonへpluginを入れると他用途も影響するため禁止。将来の専用daemonでのみ検討|最も高い。EngineバージョンごとのAPI差分、失敗時拒否、ストリーム制御、回帰試験が必要|単独の代替境界にしない。限定ブローカーの追加防御候補|

Docker公式ではrootless時の資源制御にcgroup v2とsystemdが必要とされる。
`--cpus`等を設定しただけで強制済みとはしない。
[Docker rootless tips](https://docs.docker.com/engine/security/rootless/tips/)

gVisor公式はrootless実行に複数の経路を区別している。Docker/rootlesskit経由と
runsc単体の条件を混同せず、daemonからruntimeまでhost rootで動かないことと、
network noneの実効性を確認する。host networkを必要とする構成は採用しない。
[gVisor rootless](https://gvisor.dev/docs/user_guide/rootless/)

Docker認可pluginはJSON以外のbodyを判断材料にできず、HTTP Upgrade後の通信は
初回の認可だけになる場合がある。attachの内容・サイズ・終了をplugin単独で保証せず、
ブローカー側のbounded I/Oが必要である。
[Docker認可plugin](https://docs.docker.com/engine/extend/plugins_authorization/)

## 推奨境界と責任

```text
ブラウザ → FastAPI（Firebase、現在のTeam/Project認可）
          → 将来のmTLS＋署名済みRUN_PYTHON
            → 専用ホストの限定ブローカー（worker Service、SQLite、監視）
              → host nativeの専用daemon接続部
                → rootless Docker → runsc → 学生Python（非root、network none）
```

図は設計のみ。FastAPIからソケット、SSH、sudo、任意URLやDocker引数を渡すAPIは作らない。
ブローカーはhost nativeの専用サービスとして検討し、Docker socketのコンテナへの直接
マウントは使わない。daemon接続部だけが専用OSユーザーのローカルsocketにアクセスする
将来設計であり、このPRではsocket接続コードもパス設定も追加しない。
rootful案の特権接続部も、APIプロセスと別の専用hostに閉じ込める必要がある。

学生コードは固定imageのPythonへstdinで渡し、ホスト上のexec/subprocessで評価しない。
学生コンテナにはhost workspace、Docker socket、秘密、管理台帳をmountしない。
0.5CPU、128MiB、追加swapなし、pids32、read-only、tmpfs/shm上限、cap全削除、
no-new-privileges、非root、network noneを維持し、runsc不在・未確認なら拒否する。
専用OSユーザーのhomeにもアプリDB/Firebase/SSH鍵/共有Gitを置かない。

ネットワークはAPI→ブローカーの認証済み限定経路だけを将来許可する。学生環境からは
管理ネットワーク、metadata、API、DB、他学生環境へ通信できないことを別途実測する。
ブローカーから既存DBや共有Gitに接続する必要はない。ソケットのro mountもアクセス
制限の代替ではなく、採用しない。

## 限定操作と認証

既存のExecutionCommand/Replyを維持し、外部に公開する操作はRUN_PYTHONだけ。
学生IDはFirebase認証後のDBユーザーから確定し、現在の所属とProject対応を照合する。
API署名keyだけを持つ未確認peerを信用せず、将来のmTLSで期待するサービスidentityと
署名を結び付ける。証明書失効・期限・nonce再利用・未知peer・通信障害は拒否する。
HTTPヘッダーによるpeerの自己申告は禁止。実mTLSとkey配備・rotationは未実装。

認証済みAPI自体が侵害された場合、署名は利用者の認可の正しさを証明しない。
短期許可、主体別rate limit、監査、所属失効の扱いを別途検討する必要がある。
学生コードや鍵、内部パスをログへ出さず、結果は既存6フィールドの契約へ限定する。

ブローカーが将来使うEngine操作は固定create/attach/start/send/read/inspect/kill/remove
に限定する。クライアントからURI・method・image・mount・Cmd・Env・label・任意IDを
受け取らない。exec、build、pull、volume/network管理、prune、force remove、daemon操作、
任意API転送は実装しない。管理者復旧もHTTP非公開のままとする。

create時にserver生成specを固定し、停止/削除前には台帳の完全ID、owner、実行ID、nonce、
generation、daemon/image/specを再照合する。ラベル検索だけで既存resourceを採用しない。
専用daemon内であっても他のコンテナへ操作権が生じるわけではない。

認可pluginを追加する将来案では、APIバージョン/HTTP method/path/query/bodyを厳格に
正規化し、重複キー・未知field・不明操作・plugin障害を拒否する。高権限接続部を迂回する
socket経路を閉じ、台帳による所有確認を行う必要がある。単なるURL prefix制限では不足。
これは要件であり、このPRに実pluginやHTTP転送器は含まれない。

## 既存workerとの整合性と残る障害

既存Driver.readyはrootful=Trueの確認を要求する。**今回これをrootless対応へ変更しない**。
将来は別の検証済みprofileを明示導入し、台帳のdaemon/image/profile束縛を移行する設計と
テストが必要。rootfulの検証結果をrootlessへ流用したり、既存台帳を書き換えたりしない。

同じ専用SQLiteを共有するprocessだけで全体4枠を予約する既存仕様を維持する。
UNKNOWNや未完了は枠を保持。create応答消失で完全ID不明なら再作成・推測削除をせず
管理者照合が必要。別daemon/別台帳への変更で受付を復活させない。
Linux fcntlと8processの実検証は、Ubuntu環境未準備のためNOT_RUNのままである。

5秒はstart直前からの単調時計上の実行期限で、全Docker操作のハングを中断する保証では
ない。制御/stream I/Oの実期限、独立watchdog、worker死亡後の所有確認付き停止は未実装。
新しい権限分離方式だけではこれらは解決しない。結果不明ならUNKNOWNと受付停止を維持。

## 静的検査の実装

`execution_worker/privilege_boundary.py`は独立した計画用の純粋関数のみ。
BoundaryPlanは設計値、review_planは危険な設定の固定エラーコードを返す。
rootless専用daemon、rootfulブローカー、daemon認可の比較候補を表現できるが、全候補に
専用host/daemon、限定ブローカー、mTLS計画、所有台帳、UNKNOWN枠保持を要求する。

root権限/socket mount/無制限転送/広範sudo/host実行/runc fallback/有効化要求、
不明方式、型混同、4枠変更を拒否する。合格相当でも状態はDESIGN_ONLYであり、
execution_enabledは必ずFalse。実機の自己申告をPASSへ変換する仕組みはない。
既存ServiceやAPIからimportせず、実行無効ゲートを置換しない。環境変数やファイルも読まない。

静的検査で拒否項目がないことは、証明書・daemon・user namespace・資源制限が
実際に安全だという証明ではない。実OSの強制や攻撃耐性は別のNOT_RUN項目である。

## 次の検証段階と停止条件

1. 承認済みの独立Linux検証環境と既存依存Pythonを用意し、Dockerなしでfcntl専用3件と
   8process予約、異常終了、既存API回帰を実行する。依存不足ならインストールせず相談する。
2. rootless daemon/runtimeの全プロセスのhost UID、user namespace、subuid/subgid、
   cgroup v2/systemd委譲、実CPU/memory/pids/swap/tmpfs上限を検証する計画を承認する。
3. 別途承認後にのみrunsc/image/ネットワーク遮断、秘密非混入、所有照合、実停止を実測する。
   自動fallback、未知resource操作、強制力の不明、計測不能なら停止する。
4. mTLS認証、限定操作ブローカー、profile分離、実I/O期限と独立監視を個別に実装・レビュー。
   全条件を満たすまではDisconnectedEngine/DisconnectedWorker/HTTP 503を維持する。

本書には導入・sudoers変更・daemon/socket変更・コンテナ操作の実行コマンドを含めない。
既存Compose、volume、PostgreSQL/DDL、共有Git、workspace、Firebase、個人エディタ、KVMを
変更せず、実機試験なしを動作確認済みと扱わない。

## テスト結果

Windowsの既存Python環境で以下を実行した。新規検査はモックのみ、既存workerは
一時SQLiteと合成Engineのみ。依存インストールなし。

```powershell
# backendで実行。実DBやDocker向け検証先を指定しない。
$env:PROJECT_TEST_DATABASE_URL = ''
$env:EDITOR_LAB_IMAGE_ID = ''
..\.venv\Scripts\python.exe -B -W ignore::DeprecationWarning -m unittest discover -s tests -p 'test_execution*.py' -q
```

- **110件中107 PASS、0 FAIL、3 SKIP**（20.377秒）。
- 新規の設計検査9件はすべてPASS。
- 既存worker/API/clientの101件中98 PASS、Linux専用3件SKIP。
- 既定Engine/Workerの拒否、既定HTTP 503、Firebase認証/所属確認、4枠予約、
  出力/期限/異常終了/復旧の既存モック試験を維持した。
- Linux fcntl専用3件はWindowsではSKIPとなったため、Linuxでの実証はNOT_RUN。
- 実Docker/rootless/runsc、実資源強制、実mTLS/認可plugin、実5秒停止はNOT_RUN。
  これらは今回収集した110件には含まれない実機試験であり、SKIP件数へ加算しない。
- 既存Starlette TestClientの非推奨警告は残る。パッケージを更新して解消する作業はしない。

既存追跡ファイルは変更せず、静的検査・テスト・本書の新規3ファイルだけを追加した。
テスト成功は権限分離の実機合格や公開実行の許可を意味しない。

## 公式資料（2026-11-19確認）

- [Docker rootless概要](https://docs.docker.com/engine/security/rootless/)
- [Docker rootlessの資源制御条件](https://docs.docker.com/engine/security/rootless/tips/)
- [gVisor rootlessの実行経路](https://gvisor.dev/docs/user_guide/rootless/)
- [Docker認可pluginの契約と制限](https://docs.docker.com/engine/extend/plugins_authorization/)

各公式仕様の適用可能性は対象host/版で再確認する。推奨案と残存リスクの評価は本設計の判断。
