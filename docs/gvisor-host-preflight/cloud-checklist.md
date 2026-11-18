# OCI Always Free適合性チェックリスト

確認日：2026-11-18。以下は公式資料の調査と計算だけで、Console・契約account・実machineの確認はNOT_RUN。無料利用や課金ゼロを保証しない。PR #13〜#15のKVM比較とgVisorの残存リスク評価を維持する。

## 最新の公式条件と保守的な試算

[Oracle Always Free公式資料](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)はA1の月1,500 OCPU時間・9,000 GB時間、常時相当2 OCPU・12GB、boot/block合計200GB、volume backup合計5件を掲載している。無料対象はhome regionなどの条件があり、在庫不足もあり得る。object storageはaccount状態・tierごとに扱いが異なるため、Always Freeのみのaccountでは合計20GBという条件を超えない計画が必要。公式ページ内のboot容量記述には47GBと50GBが混在しており、準備では少なくとも大きい50GB側を前提にし、実際の選択画面で再確認する。

同資料では7日間の低利用率に基づくidle instance回収も説明される。学校の授業間・休暇・少人数利用で回収リスクがあり、回収回避用の人工負荷は提案しない。Free Trial終了時のobject容量超過によるデータ喪失条件も確認し、無料枠だけをbackup保証にしない。

|予約案|CPU・RAMの計算|現時点の評価|
|---|---|---|
|host/runtime余裕|1CPU枠＋2GiBを確保|計画値。CPUとcloud OCPUの対応、GB/GiBの差を実設定で確認|
|1人の検証候補|editor1CPU＋2GiB、合計2CPU枠＋4GiB|計算上の候補。性能・強制・実リソース取得は未確認|
|従来基準2CPU＋4GiB|host込み3CPU枠＋6GiB|同CPU枠では不足。自動overcommitしない|
|2人：各1CPU＋2GiB|host込み3CPU枠＋6GiB|同CPU枠では不足。無料2人運用可能とは判断しない|
|4人：各1CPU＋2GiB|host込み5CPU枠＋10GiB|不足。4は最大許可数で、予約不足なら起動拒否|

[code-serverの推奨目安](https://coder.com/docs/code-server/requirements)は1GB RAM・2CPU coresであり、1CPU案はこのCPU目安より小さい比較試験である。gVisorとPython/GCC/Node/LSPの併用性能はNOT_RUN。使用量上限の実測、host余裕、OOM回復を確認してから判断する。

個人data20GiB＋root/image予約16GiB＝1環境36GiB。hostを50GiBと保守的に見積もると、1人86GiB、2人122GiB、4人194GiBで、4人分は200GB（約186GiB）を超える。さらに空き20%・inode・thin metadata・logs・image更新・backup stagingを確保するため、見かけ上収まる値でも許可しない。

外部backupは別途必要。20GiBは約21.5GBで、本人data満杯の完全backupは20GB枠を超える。圧縮・差分・共有image削減を未測定のまま無料成立の根拠にしない。5件のmanaged volume backup上限とobject storageは別枠であり、同じbackup方式と混同しない。保存世代数、backupの暗号鍵管理・restore先容量・外部取得費用も算入する。

## ARM64/rootless/runscの条件

[gVisor公式rootless資料](https://gvisor.dev/docs/user_guide/rootless/)に沿い、caller設定usernsを使い、非rootなdaemon/runtime、本人ごとのsubuid/subgid、先行したnetwork namespace分離を検査する。組込み`--rootless`にはcgroup等の制約があるので、そのフラグの存在を合格にしない。[systemd driver](https://gvisor.dev/docs/user_guide/systemd/)ではsystemd 244以上とunified cgroup v2が条件になるが、cpu/memory/pidsの委譲・適用を実証する必要がある。

[systrap](https://gvisor.dev/docs/user_guide/platforms/)にKVM deviceは不要だが、A1でuserns/LSM、kernel、quota、network、採用release bundleが実際に対応するかはNOT_RUN。ARM64 image・Node native addon・拡張・syscall対応も個別試験する。KVMと同等の隔離とは扱わない。

## 課金・運用の確認票（今回はすべて未確認）

|確認項目|将来、担当者が記録するもの|停止条件|
|---|---|---|
|account種別と契約|Free Trial/Always Free/PAYG、学校の許可、最新規約|不明・未承認なら契約/作成しない|
|region/shape/総使用量|対象ラベル、既存account全resourceを含むquota、作成画面の見積り|対象外、空きなし、上限不明なら停止。既存resourceを消して空けない|
|有料resource|boot/blockの性能tier、backup、image、object requests、network/NAT/log等の料金|許容費用または無料対象であることを確認できなければ停止|
|作成権限とquota|専用compartment、最小権限、許可shape・容量の制限案|超過作成/自動resize/有料fallbackを防げなければ停止|
|監視|利用量と請求確認の担当・頻度・通知先|通知だけで課金停止とみなさない|
|idle回収と障害|独立backup、復元可能なimage/bundle、再取得不可時の代替計画|dataを失う、復元先容量なしなら停止|
|学校30〜40人|複数チーム、User×Project数、保存割当、費用責任者|800GiB以上のdata/backupを無料枠へ押し込まない|

[Oracle Budgets](https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/budgetsoverview.htm)はsoft limitと定期評価の通知であり、課金のhard stopではない。[Compartment Quotas](https://docs.oracle.com/en-us/iaas/Content/Quotas/Concepts/resourcequotas.htm)はresource使用制御の候補だが、すべての課金対象を止める保証はない。今回はquota・IAM・予算通知も設定しない。

40人×20GiBで本人dataだけ800GiB、複数Projectならさらに増える。進捗管理/API/DB/Gitレビューとeditorを別の負荷として見積もり、全体同時4枠は維持する。将来10〜20同時は複数host・共通予約・fencing・backup・認証失効の検証が必要。学校限定の登録規則を実行方式へ固定しない。無料枠は最初の1人の成立可能性を調べる候補で、本番の容量・可用性・料金の保証ではない。
