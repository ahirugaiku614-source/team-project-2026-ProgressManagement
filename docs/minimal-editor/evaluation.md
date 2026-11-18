# 最小構成・隔離方式・無料クラウド評価

2026年11月18日の公式資料を参照した。推定と実測を混同せず、以下の全構成は実機NOT_RUNである。
root16GiB/data20GiBの基準は維持する。将来縮小する場合も使用量・更新・backup復旧を測って別承認する。

## 最小スペック比較

code-serverの[公式要件](https://coder.com/docs/code-server/requirements)は最低推奨1GB RAM/2 CPU cores。
これはPython/GCC/Node/拡張を同時に使う今回の授業環境の保証ではない。
1 CPUを非対応と断定はしないが推奨CPU未満の実験として扱う。メモリを減らすだけで安定性が増すとは考えない。

| CPU/RAM | ブラウザエディタ | Python | C/GCC | Node.js | Git | 判断/実測 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 CPU/1GiB | CPU推奨未満。language server/拡張でOOMの懸念 | hello/標準lib単体候補。巨大データ/追加libraryなし | 単一file・-O0・逐次候補。大規模build不可と仮定せず未評価 | 小script候補。npm install/buildは別の大きな負荷 | 小repoのlocal status/diff候補。clone圧縮は別測定 | 最低優先の探索枠、全NOT_RUN |
| 1 CPU/2GiB | RAM余裕は増えるがCPU推奨未満 | editorと軽いscriptの併用候補 | 単一fileの逐次compile候補 | JS実行候補。依存downloadは許可しない | 小repo候補 | 1 CPU節約の効果を測る枠、全NOT_RUN |
| 2 CPU/2GiB | 公式CPU/RAM目安を超える。拡張を限定して比較 | editor+標準libの初回検証候補 | -O0/逐次buildから開始 | 単純実行。language server/巨大buildは未保証 | 制限付きlocal操作を計測 | **次の実機比較の第一候補**、全NOT_RUN |
| 2 CPU/4GiB | 従来基準。拡張の無制限導入はしない | 軽い授業処理の比較基準 | GCC逐次/並列負荷を別試験 | 従来の比較基準 | 同左 | 安全余裕の比較基準、全NOT_RUN |

ブラウザUIの描画は生徒PC側、serverのファイル操作・PTY・language server・compilerはサーバー側で測定する。
使用できそうという評価はインストール/動作確認ではない。root16GiB内のOS・toolchain・更新前後の実使用量も未測定。
swapでRAM不足を隠さず、OOM/長時間停止/応答悪化を記録して不合格にする。
オフラインで固定した依存物を使い、無料枠試験のためにInternet/任意port転送を開けない。

## 隔離方式

| 方式 | 境界 | 小型化で注意する点 | 採用前に必要な確認 |
| --- | --- | --- | --- |
| KVM/libvirt | 専用Linux guest kernelとdisk/NIC | QEMU/host予約、kernel/root更新、nested対応が必要。小さいguestでもhost予約は消えない | 外側ACL、metadata/DB/Git/管理socket拒否、非root、guest TLS、disk専用RW、fencing/readyの実証 |
| rootless＋runsc systrap | 非root daemon/runtime/Sentry/Goferとsyscall仲介。host kernelを利用 | CPU/IO syscall overhead、別UID/cgroup/限定mount、ARM拡張互換性。通常runcへfallback禁止 | KVMと同等とは未認定。別脅威モデル、host共有DoS/脱出、guest外相当の通信/保存境界、更新体制のレビュー |
| Firecracker等microVM | 専用guest kernel、KVM、縮小device model | kernel/rootfs/agent/jailer/TAP管理が増える。VM用RAMを減らしてよい根拠は未測定 | /dev/kvm利用条件、jailer相当制限、専用block、外側ACL、checkpoint/crash/世代切替の安全性 |

根拠：gVisorの[security model](https://gvisor.dev/docs/architecture_guide/security/)、
[rootless](https://gvisor.dev/docs/user_guide/rootless/)、[platform](https://gvisor.dev/docs/user_guide/platforms/)、
Firecrackerの[本番要件](https://github.com/firecracker-microvm/firecracker/blob/main/docs/prod-host-setup.md)。
rootlessだけ、共有code-serverのパスワード分離、単純Dockerだけで任意コードを公開する案は採らない。
既存[追加比較](../school-pilot/isolation-cloud-comparison.md)の停止条件も維持する。

## Oracle Always Free：1/2/4人

[現行公式](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)はA1合計2 OCPU/12GB相当、
1500 OCPU h/9000 GB h、boot/block合計200GB等。古い4 OCPU/24GBの資料を保証に使わない。
OCPU/GBの実際の単位・適用tenancyとregionを確認し、2CPUがx86の2threadと同等性能とは扱わない。

| 人数 | 計算上の可能性 | 保存と隔離に残る条件 | 判定 |
| --- | --- | --- | --- |
| 1 | 直接A1 VMの2 CPU/2〜4GiBは総量内候補。共有runsc hostの制御余裕を引くと1 CPU案しか予約できない場合がある | A1 nested KVMは公式確認できず未確認。直接VMのmetadata外側遮断も未確認。ARM toolchain/拡張・backup・管理系別枠が必要 | **可能性の検証対象、運用可とは未判定** |
| 2 | 1 CPU/1〜2GiB×2はraw CPU/RAM総量内。各2 CPUの2人はCPU超過 | 1 CPUはcode-server推奨未満。2つの直接VMなら各boot50+data block50GBで200GBを使い切る。共有runscにはhost予約が別途必要 | **強い条件付き、未検証** |
| 4 | 最小の1 CPU×4でもraw2 OCPUを超える | RAMだけ足りてもCPU/boot/data/backup不足。oversubscriptionや上限回避で成立扱いにしない | **この予約基準では不可** |

常時2 OCPU×730h=1460 OCPU hでも、管理系の利用や他インスタンスを加算すると無料時間を超え得る。
4人を交代利用させることは4人同時利用ではない。30〜40人のWeb/API/DBをこの小さなhostへ同居させない。
無料A1の在庫不足・idle判定による回収があるため、唯一のデータ保存先とせず、回収回避の無意味な負荷も発生させない。

[boot/block仕様](https://docs.oracle.com/en-us/iaas/Content/Block/Tasks/creatingavolume.htm)の最小50GBを考慮する。
root16/data20GiBという論理quotaを、実際に16/20GBのOCI diskとして作れると推定しない。
直接VMで各boot50GB+別data50GBなら1人100、2人200、4人400GBとなる。
同じboot内でroot/dataを分ける節約は、復旧・quota・root更新の境界変更になるため今回は設計/実装しない。

ARM64はcode-serverの[standalone配布](https://coder.com/docs/code-server/install)等に対応候補があるが、
Python native wheel・GCC・Node・extensionを含む固定イメージ全体は未確認。
gVisorの[対応環境](https://gvisor.dev/docs/user_guide/install/)も特定cloudでの合格証明ではない。
systrapはnested KVMを前提にしないが、user namespace/cgroup/ネットワークの条件を実測する必要がある。

## バックアップ容量・費用

圧縮なし・編集環境のみ2世代・manifest/base等32GiBとして、論理root16+data20の例：

| 保存環境P | 本人root+data | 外部backup 2世代+32 | $6.95/十進TB月の参考保存費 |
| ---: | ---: | ---: | ---: |
| 1 | 36GiB | 104GiB | 約$0.78/月 |
| 2 | 72GiB | 176GiB | 約$1.31/月 |
| 4 | 144GiB | 320GiB | 約$2.39/月 |
| 40 | 1440GiB | 2912GiB | 約$21.73/月 |

[B2公式価格](https://www.backblaze.com/cloud-storage/pricing)による単純換算で、税・為替・転送・操作・鍵/運用費は別。
app/DB/Gitのbackup128GiBはこの小型編集試験表に含めず、P=40では従来計画3040GiBを維持する。
直接OCI VMでbootも最大50GiB相当を保全するなら、(50+20)×P×2+32で
1人172、2人312、4人592GiBに増え、約$1.28/$2.33/$4.42/月となる。実GB/GiBは調達時に照合する。
無料のhome region volume backup5個を、別障害領域・暗号鍵・復元済みの外部backupと同一視しない。
無料object20GBだけでは上表の外部コピーを収容できず、完全無料の保全を確認済みとはしない。

初回作成は最初のdisk消費を遅らせるが、停止中も個人データを保持するため、
登録40人が各1Projectを使った後の保存量は小型CPU/RAMにしても減らない。
容量不足時は新規作成/起動を拒否し、古いdiskを削除・移行して空けない。
Google/Azure等の無料/学生枠、規約/期限・回収については[第11段階比較](../school-pilot/isolation-cloud-comparison.md)も参照する。
現在はクラウド契約も実機適合性確認も行っていない。
