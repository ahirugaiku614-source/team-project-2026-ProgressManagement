# 第13段階：rootless＋gVisorエディタ検証基盤

この段階は**独立した参照実装とモック試験だけ**である。コンテナのbuild・起動、実runsc、ブラウザ接続はすべて **NOT_RUN**。KVM専用VM方式を維持し、gVisor方式を別候補として評価する。一般公開できる隔離を確認した成果物ではない。

## 成果物と境界

|対象|今回追加したもの|未実装・未検証|
|---|---|---|
|設定|runsc/systrap・caller設定userns・非root・制限付き設定の生成と完全一致検査|OCI変換、rootless engine、実設定の強制確認|
|ライフサイクル|起動意図、予約、模擬inspect、保存付き停止、再起動、隔離|実コンテナ操作、実ジョブ検出、buffer保存、障害環境の自動復旧|
|台帳|専用一時SQLite、操作ID、generation、30秒lease、複数プロセスの排他|本番ストレージ、複数ホスト、HA、実ワーカーとの分散fencing|
|イメージ|Dockerfile、限定build context、無効entrypoint、設定例|承認済みdigest、build、SBOM、脆弱性検査、各architectureの動作|
|接続|接続しないことの契約、将来経路の設計|HTTPS、認証連携、HTTP/WebSocket、リレー|
|セキュリティ|不正設定・証跡・所有者・世代の拒否テスト|実パケット遮断、syscall隔離、脱出耐性、実quota|

追加ファイルは `backend/gvisor_lab/`、`backend/tests/gvisor_fakes.py`、`backend/tests/test_gvisor_lab.py`、`artifacts/gvisor-editor/`、この資料群だけ。既存KVM、VM manager、minimal_editor_lab、gateway、Firebase、FastAPI、Compose、DB/migrationは変更しない。共通の`EditorBinding`と既存`Profile`/`Budget`型は読取り利用する。

## rootlessの方式を取り違えない

[gVisor公式rootless資料](https://gvisor.dev/docs/user_guide/rootless/)（2026-11-18確認）は組込み`runsc --rootless`について、呼出しUID以外のmapping、create、保存復元、network、cgroup制約を挙げている。特にcgroup設定エラーが無視され得るため、これを付けるだけの起動例は採用しない。

今回の候補は **callerがuser namespaceを構成する方式**。将来の専用ホスト上でrootless engineとrunscの組合せを固定・検証する。daemon/runtimeも非rootで、各User×Projectに別のhost principalと重複しないsubuid/subgid範囲を割り当てる設計とする。ホスト側の限定されたUID mapping helperが必要な場合は別途レビューし、コンテナへsetuidや管理権限を渡すこととは区別する。rootful Dockerのdockerグループや`userns-remap`だけではこの条件を満たさない。

`runtime=runsc`、`platform=systrap`を明示し、未導入・不正digest・未確認設定では拒否する。runc/ptrace/KVM platformへのフォールバックはない。[systrapはKVM platformと異なり`/dev/kvm`を要求しない](https://gvisor.dev/docs/user_guide/platforms/)が、無料ホストでの実動作を保証しない。

## 設定契約

`plan_for()`は**独自のLAB_PLAN**を返す。Docker SDK引数や完成したOCI bundleではない。任意コマンド・任意mount・パスを受け付けるAPIは追加していない。

- 実行UID/GIDは1000:1000、privileged=false、capability全削除、no-new-privileges、root filesystemは読取り専用。
- 本人専用`OWNER_DATA:<volume ID>`だけを`/workspace`へ、本人設定`OWNER_CONFIG:<sandbox ID>`だけを読取り専用でmountする。logical IDはホストパスではない。安全なresolverは未実装。
- Docker socket、ホストroot、鍵、認証情報、共有Git、project_storage全体、他者データ、device、host namespaceを許可しない。
- `network=none`、公開portなし。将来も原則egress拒否。管理網、DB、共有Git、他sandbox、IPv4/IPv6 metadataを遮断する。
- 比較対象ProfileのCPU/RAM、pids=256、root枠16GiB、本人data枠20GiB。CPU overcommitはしない。停止後もdisk枠を予約する。
- `/tmp`64MiBとHOME32MiBだけをnosuid/nodev/noexecのtmpfsにし、メモリ予算内に計上する。workspaceはコンパイルした本人プログラムの実行が必要なのでnoexecにはしない。
- `swap_bytes=0`は**cgroup v2のmemory.swap.max=0という契約**。Dockerの`--memory-swap=0`へ機械的に変換すると無効/default扱いになり得るので禁止。

rootless環境ではcgroup v2のdelegationとcpu/memory/pids controllerの実強制が必須。[systemd cgroup driverの公式条件](https://gvisor.dev/docs/user_guide/systemd/)も確認し、対応するsystemd・カーネル・runtimeを固定する。root/data quotaはホスト側の専用quota領域で強制する設計とし、単なるコンテナ内`du`やCPU/RAM設定値の表示を合格証跡としない。

`Capabilities`は有効期限5秒の**モック証跡型**。署名・ホストprobe・mTLSの実装はなく、利用者が送るJSONをこの型へ変換して本番許可してはいけない。UID、digest、storage、network、userns、全制限が確認済みという仮定が欠けると予約を拒否する。実quota/filesystemへの安全なFD解決、symlink/hardlink拒否、subuid割当は今後の作業である。

## イメージとブラウザ接続

Dockerfileは将来の公式code-serverのレビュー済みmultiarch digestを`BASE_IMAGE`に指定する前提。既定値・承認済みdigestはまだなく、manifestにもnullと記録する。Python/GCC/Node.js/npm/Gitを導入するレシピだが、aptパッケージを固定しておらず再現可能buildや安全な最新版を保証しない。Nodeの対応version、C拡張、Language Server、Git local利用、code-server拡張の動作は未検証。

root指定はbuild内だけ。実行時はUSER 1000:1000。setuid/setgidビットとsudoをイメージ内で除く設計であり、既存ファイルの権限は変えない。build contextはこの小ディレクトリに限定し、`.dockerignore`は必要ファイルだけ許可する。`.env`や鍵をCOPYしない。

**entrypointは常にexit 78で拒否する。** code-server設定例はauth=passwordで、共通パスワードを含めない。将来は本人専用の短期credentialを読取り専用設定として注入し、URL・環境変数・ログで漏らさない。生成・失効サービスは未実装。

現設定のnetwork none＋loopback bindではブラウザから接続できない。これは意図した無効状態である。将来は独立HTTPS gateway→認可済み本人専用リレー→当該sandboxという経路を別レビューで追加する。公開portやhost networkで迂回しない。必要通信だけのprivate経路と外側egress firewallの両方を実証してから接続可能にする。

既存gatewayのFirebase失効確認・現在のTeam/Project所属・短期セッション・Host/Origin・WebSocket再認可の条件を引き継ぐ。ただし現在の`VMRoute`/`safe_ready`へ変換しない。新しいsandbox用route契約と実証跡の検証が必要。今回の成功結果は常に`execution=MOCK_ONLY`、`connectable=false`で、接続URLはない。

## 脅威モデルと残存リスク

利用者はターミナルから任意プログラムを実行できる攻撃者と仮定する。usernsと非rootだけで隔離完了とは扱わず、Sentryによるsyscall仲介、限定Gofer、mount限定、network遮断、host principal分離、quotaを組み合わせる。

[gVisorのセキュリティモデル](https://gvisor.dev/docs/architecture_guide/security/)はKVMの専用guest kernelと異なる。Sentry/Gofer/runtime、共有host kernelの脆弱性、side channel、hostのDoS、設定ミス、供給網攻撃は残る。学校内限定でもこのリスクはなくならない。KVMと同等の安全性を確認済みとは扱わず、従来のKVM設計を残す。

## 検証の実行方法

Windows PowerShellで新しいworktreeの`backend`を作業ディレクトリにし、既存の開発用Pythonを使う。以下はファイルと合成データだけを扱う。

```powershell
python -m gvisor_lab.check
python -W ignore::DeprecationWarning -m unittest discover -s tests -p test_gvisor_lab.py -v
```

実操作引数`--run`/`--build`は存在しない。静的検査は既存の保護hash、レシピhash、manifestの未実施状態、設定allowlistを検査するだけ。`STATIC_CONSISTENT_ONLY`でも配備を許可しない。

今回の試験結果・回帰確認は[検証手順](validation.md)、資源計算と公式情報は[クラウド評価](cloud.md)を参照する。
