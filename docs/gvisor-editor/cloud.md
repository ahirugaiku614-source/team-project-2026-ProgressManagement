# 無料クラウド・architecture適合性（設計評価のみ）

公式情報の確認日：2026-11-18。契約・Console確認・インスタンス取得・実測は行っていない。無料枠や在庫は変更されるため、実検証前に再確認する。PR #13/#14のKVM・費用・学校40人の保存容量計画は維持する。

## OCI Always Free

[Oracle公式Always Free資料](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)の現在の掲載値はA1 ARMで月1,500 OCPU時間・9,000 GB時間（常時相当2 OCPU・12GB）、boot＋block合計200GB。古い4 OCPU/24GBの紹介値で成立判定しない。home region、空きshape、アカウント適格性、minimum boot、idle回収が制約になる。

単位はクラウドのOCPU/GBとアプリのvCPU/GiBを無条件に同一視せず、以下は**2つのARM CPU枠と12GiB以下を仮定した保守的な試算**。本体Web/API・DB・HTTPS gatewayはこのsandbox hostへ同居させない。別途費用・リソースが必要である。

|計画|CPU予約|RAM予約|判断|
|---|---|---|---|
|host/runtime/監視の余裕|1 CPU枠|2GiB|仮置き、計測で増やす可能性|
|1人：1 CPU/1GiB|合計2|合計3GiB|予約計算は成立。OOM、LSP、GCC、Node性能は未実測|
|1人：1 CPU/2GiB|合計2|合計4GiB|最初の検証候補。実動作はNOT_RUN|
|1人：2 CPU/2〜4GiB|合計3|合計4〜6GiB|この余裕を維持するとCPU不足で拒否|
|2人：各1 CPU/2GiB|合計3|合計6GiB|同じ予算ではCPU不足で拒否。CPU余裕を外して無料成立としない|
|4人：各1 CPU/2GiB|合計5|合計10GiB|同無料CPU予算では不可|

ホスト用余裕を小さくした2人計画は将来の別検証案であり、過負荷時にも隔離・停止・認証制御が成立する実測なしに推奨しない。初期上限4は最大許可数で、資源不足なら1/2台目でも拒否する。将来10〜20人は有料の複数専用hostと共通予約機関が必要で、無料運用を保証しない。

公式資料のboot最小47GBも含め、計画ではhost rootを50GiB程度確保する。sandboxごとのroot16＋data20GiBは保守的に共有image削減を差し引かず計上する。cloud GBとの換算も余裕を取る。

|環境数|本人root/data予約|同量の完全backupを同じ容量内に置いた場合＋host50GiB|
|---|---|---|
|1|36GiB|122GiB|
|2|72GiB|194GiB：200GB（約186GiB）を超過|
|4|144GiB|338GiB：超過|

backupは同host障害から独立させる必要があるため、この表の同容量配置は最終案ではない。公式object storage無料枠20GB、volume backup数の制約があり、本人data20GiBの完全backupですら圧縮率を仮定すると危険。backup/restoreを含めた無料完結は未成立。データ増加と世代保持で追加費用が必要になる。40人×20GiB＝800GiBは無料200GBに収まらず、User×Projectが複数なら更に増える。thin provisionや初回作成もこの論理予約を消さない。

idle回収や在庫不足に対して、復元手順、別hostへの所有権・generation引継ぎ、外部backupが必要。回収を避ける人工負荷は提案しない。学校からの利用、任意コード、ネットワーク制限、Free Tier規約・最新利用規約は運用者が確認する。費用上限の設定だけで課金や復旧費を保証しない。

## ARM64とx86_64

|項目|ARM64|x86_64|
|---|---|---|
|runsc|公式配布/対象architectureだが、採用release・systrap・kernelを実機検証|同様。既存Docker Desktopで代用しない|
|code-server|multiarch公式imageの当該digestと拡張のnative依存を検査|当該digestと拡張を別に検査|
|Python/GCC/Git|ディストリビューションの対応packageをbuild後に確認|同様|
|Node/npm|LTS整合、native addon、language serverのARM対応を検査|同様。x86のみの拡張にも注意|
|syscall/性能|[Linux/arm64互換表](https://gvisor.dev/docs/user_guide/compatibility/linux/arm64/)と実toolchain試験が必要|[Linux/amd64互換表](https://gvisor.dev/docs/user_guide/compatibility/linux/amd64/)を別に確認|

[code-server requirements](https://coder.com/docs/code-server/requirements)の1GB/2CPU程度という推奨は、gVisor上でPython・GCC・Node・拡張を同時利用できる保証ではない。1CPU/1GiBは限界測定用、1CPU/2GiBは小規模検証候補、2CPU/2GiBはCPU優先、2CPU/4GiBは従来基準として比較を続ける。すべて実性能は未測定。

## 実行方式とhost前提

|方式|この段階での扱い|無料クラウドでの障壁|
|---|---|---|
|KVM/libvirt|従来の専用guest kernel設計を維持|nested virtualization・必要device・余裕CPU/RAMが未確認|
|rootless＋runsc/systrap|今回の独立参照実装|caller userns、非root principal/subuid、delegated cgroup v2、quota、network分離をすべて実証する必要|
|Firecracker等microVM|比較候補のまま、実装しない|KVM device、guest lifecycle、nested条件が別途必要|

gVisorに`/dev/kvm`が不要でも、クラウドのカーネル設定、seccomp、unprivileged userns制限、LSM、cgroup委譲、ストレージquotaが揃うとは限らない。最低kernel番号だけで合格にせず、保守中のLinux（検討値kernel 6.1以降）、採用runsc release、systemd/userns/cgroupを組合せで固定して試験する。runsc built-in rootlessのcgroup制限を回避するため制限を弱める案は採用しない。

追加の一次資料：[gVisor install](https://gvisor.dev/docs/user_guide/install/)、[rootless](https://gvisor.dev/docs/user_guide/rootless/)、[cgroup driver](https://gvisor.dev/docs/user_guide/systemd/)、[code-server install](https://coder.com/docs/code-server/install)。Google/Azure等の期間付きクレジットは永続無料の根拠にはせず、前段階の比較を維持する。今回の結論は「専用hostで1人から検証する候補」であり、「無料クラウドで運用可能」ではない。
