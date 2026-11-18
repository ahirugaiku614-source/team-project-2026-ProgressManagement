# 追加調査：隔離方式・無料枠・学生クレジット・月額費用

## 結論と前提

2026年11月18日に公式公開資料を確認した。学校には余剰サーバーがないという追加条件を反映する。
初版の[KVM構成/保存容量](architecture.md)、[購入予算の比較](costs.md)、[安全性試験](validation.md)は維持する。
ただし初版Aの「学校所有/新規購入から開始」という優先順位は採用せず、**購入を避けた短期検証・月額調達を先に比較する**。

- 無料枠だけで、40保存環境・同時4人・現行の隔離/永続化/復旧基準を満たす構成は確認できなかった。
- 安全基準を変えずに進める初期候補は、**KVM対応の月額専用ホストを借りる方式**。
  高額な一括購入を避け、既存のUser×Project別Linux VM設計をそのまま検証できる。
  ただし必要SSD/CPUを満たす実見積りは未取得であり、「市場最安を確認済み」とは主張しない。
- 資源費の理論値ではrootless＋runscの有料クラウドを授業時間だけ使う案も安い。
  しかし専用Linuxゲストカーネルという現行要件と同一ではなく、同等性を紙上で認定しない。
  安全性レビューと別設計承認がないまま安価案へ切り替えない。
- 実機検証の自己負担を下げるには、承認済みの教育/試用クレジットで、対応SKUのKVMを
  **合成データ1〜4環境だけ**短期検証する案を先に検討する。40人の保存サービスを無料で提供する案とは区別する。
  SKU quota・学校/年齢の資格・地域・規約・予算・期限を確認できるまでは申し込まない。

本調査では契約、見積依頼の送信、VM作成、設定/認証/課金変更、データ移送は一切行っていない。
すべて設計のみで、実行/配備の許可は引き続きfalse。実機合格済みの方式はまだない。

## 共通の比較条件

登録30〜40人、同時4環境、User×Projectごとに保存する。全員が1Projectを使うP=40を費用の基準とする。
各編集環境の初期予算は2 CPU/4GiB。Web/APIは別2〜4 vCPU/4〜8GiB、現在所属/HTTPS管理系も分離する。
費用差を出すためにゲストRAMを減らしたり、CPU overcommit、共有パスワード、VMとGitの同居を前提にしない。
初版の主保存1584GiB、外部backup3040GiB、20%空き、実効2/4TiBの計画を基準として残す。
別方式でbootの最低容量等が増える場合は増額する。1人が2ProjectならP=80で別計算する。

## 方式比較

以下の必要スペックは性能実測値ではなく、元の2 CPU/4GiBを維持した比較予算である。
runsc/microVMだから4人分を無料micro VMへ詰め込める、とは推定しない。

| 方式 | 30〜40登録/4同時の初期予算 | 隔離強度と残るリスク | 既存設計との互換性 | 10〜20同時への拡張 |
| --- | --- | --- | --- | --- |
| KVM/libvirt・借用専用Linuxホスト | 12 CPU予算/32GiB、または16/64GiB、実効SSD2TiB以上＋外部backup | User×Project別guest kernel。QEMU/KVM/host管理者・side channel・DoSは信頼/残存境界。ゲスト外ACL、disk分離、非root、更新が必要 | 最も高い。managerの実アダプター、fencing/ready証跡、guest TLS等は依然未実装 | 初版Cの権威台帳/host affinityへ。初期は全体4、10/20へ自動変更しない |
| rootless＋runsc（systrap）を別クラウドLinux上で実行 | 4×2 CPU/4GiB＋予約4 CPU/8GiB、初期12 CPU/32GiB程度を比較。OS共有でも永続data/backupは残る | Sentryがsyscallを仲介。rootlessは権限境界、runscは追加syscall境界で別の役割。host kernel/Gofer/共有資源・誤mount・外側network設定のリスク。VMと同等と未認定 | Binding/コード/Cookie/現在所属の論理契約は利用可能。VMRoute/VM ID/disk・generation・管理/readyの意味をそのままcontainerへ偽装しない。別アダプター/脅威モデルが必要 | 各sandboxのcgroup/UID/保存・fenceを全体予約。syscall/IO負荷とhost共有DoSを実測。人数密度改善は未確認 |
| Firecracker等のmicroVM | guest予算を下げず初期12 CPU/32GiB、同じ保存予算から開始 | guest kernel＋小さいdevice model。ただしKVM、jailer相当制限、kernel/rootfs更新、TAP/ACL、専用block deviceが必要。host脱出・side channelは残る | VM単位の所有・世代とgatewayは近い。libvirtアダプターを流用できるとは限らず、起動job/保存/復旧/guest bootstrapは新規 | 管理/更新負担が増える。軽量化の効果を実測するまで同じ10/20台予算を適用 |
| クラウド提供VMを各User×Projectへ直接割当て | 実行4×2 CPU/4GiB以上＋別管理系。停止時もP=40のboot/dataを保持 | provider hypervisorによる別guest kernel。アカウント/IAM/metadata/agent/ディスク再割当ての追加境界。nested KVMは不要だが、guest外metadata/egress遮断を確認する | 概念は近いがcloud resource ID、operation/fence、IAM最小権限、quota/非同期job/ready証跡は新規。FastAPI/gatewayへcloud管理資格を渡さない | provider quota、disk attach、IP/NSG、予算を全体管理。小型VMを増やすだけで二重実行対策は完成しない |

通常のDocker/rootlessのみ、chroot、VS Code画面の分離だけは今回の任意コード隔離の採用対象外。
ブラウザ内WASMは安全境界候補になり得るが、任意Python/GCC/NodeやPOSIX/code-serverとの互換性を満たす根拠がなく、
今回は代替編集基盤として採用しない。学生個人PCへGit/Docker操作を要求する方式にも変更しない。

技術根拠：gVisorの[security model](https://gvisor.dev/docs/architecture_guide/security/)はVMとの異なる境界と残存攻撃面を説明する。
[rootless](https://gvisor.dev/docs/user_guide/rootless/)はdaemon/runtime/containerを通じた非rootが必要で、docker group追加の代用ではない。
[platform](https://gvisor.dev/docs/user_guide/platforms/)のsystrapはKVM platformと異なり、nested KVMを前提にしない。
[対応環境](https://gvisor.dev/docs/user_guide/install/)のx86_64/ARM64対応は、特定クラウドでの安全性合格を意味しない。
Firecrackerの[本番要件](https://github.com/firecracker-microvm/firecracker/blob/main/docs/prod-host-setup.md)はKVMとjailer相当の制限を要求する。

## Oracle Cloud

[Always Free公式](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)の現行記載は
A1合計1500 OCPU時間/9000 GB時間（無料tenancy相当2 OCPU/12GB）、boot/block合計200GB。
idle資源の回収とhome regionの在庫不足がある。**古い4 OCPU/24GBの資料を今回の保証にしない**。
公開資料間に差がある場合、適用契約/tenancyの確認前に有利な方を採用しない。

| 項目 | 判定 |
| --- | --- |
| 無料A1上の4人用KVM/libvirt・microVM | **未確認・採用不可**。A1 VMのnested KVM対応を公式資料で確認できず、元の12 CPU予算にも届かない |
| 無料A1＋rootless/runsc | ARM64/systrapに技術上の可能性はあるが**実機未確認**。同時4人のCPU/RAM予約を満たさず、40環境の容量も不足 |
| 無料のUser別クラウドVM | 2 OCPU合計と200GBで今回要件を満たさない。4人分のguest予算やP=40の保存が確保できない |
| 有料A1 | CPU/RAMを増やせるが、KVMホストとして使えるとは未確認。runsc案/直接VM案は下記単価で試算可能。ARM向けcode-server拡張・コンパイラ・イメージ互換性も未検証 |
| 学生支援 | [Oracle Academy](https://academy.oracle.com/en/solutions-cloud.html)はInstitutional member教員/学生向けのCloud Free Tier利用機会。全学生へ任意額クレジットを無条件付与する根拠はなく、枠の増量を計上しない |

直接VM方式では[IMDS公式](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/gettingmetadata.htm)が説明する
IMDSv1無効化をIMDS全遮断と取り違えない。任意コードがIMDSv2を呼べる可能性を排除できず、
**現行要件のゲスト外metadata遮断は未確認のため利用開始不可**。
インスタンスprincipalを付けないだけで合格にしない。runscの場合もsandbox外での遮断、
metadata/他ユーザーmount/管理socketに届かない証跡が必要。

[Free Tier FAQ](https://www.oracle.com/cloud/free/faq/)は個人ごと1無料アカウント、試用$300/最大30日を記載する。
クラス人数分のアカウントを集約して1サービスの無料上限を回避する案は採らない。
条件・退会/期限後の資源保持は[契約入口](https://www.oracle.com/contracts/cloud-services/)から対象地域の契約を再確認する。

## Google Cloud

[無料枠](https://docs.cloud.google.com/free/docs/free-cloud-features)は対象米国地域のe2-micro 1台分・標準disk30GB等。
[nested virtualization](https://docs.cloud.google.com/compute/docs/instances/nested-virtualization/overview)ではE2/Arm等は対象外で、
対応L1上のLinux KVMを案内している。したがって**無料e2-microでKVM/microVMは対応不可**。
runsc systrapの可能性があっても、無料micro資源で4人分を確保できるとは扱わない。

| 項目 | 判定 |
| --- | --- |
| 有料N2等のnested KVM | 対応する系列/CPU/zoneとnested設定の確認後の候補。KVMゲスト外のnetwork拒否をL1に置ける。実機/性能は未検証 |
| trial | $300/90日。無料枠そのものとは別。trial中はquota増加申請不可等があり、16 vCPU L1の作成可否を事前確認する。承認なしに有料化しない |
| 教育クレジット | [Faculty公式](https://cloud.google.com/edu/faculty)は対象教員最大$100、学生最大$50。審査/授業条件あり、金額/資格は未確定 |
| 利用形態 | [教育credit手順](https://docs.cloud.google.com/billing/docs/how-to/edu-grants)に従い学校/教員が対象授業を確認。40×$50を自由に合算できるサービス予算とは扱わない |

trial/creditは期限後に継続を保証せず、資源停止/削除リスクがある。生徒の唯一のデータ保存先にしない。
教育creditを一般公開後の恒久運用費へ計上しない。直接クラウドVM案のmetadata制御は別確認が必要で、
nested KVMホスト案の「L2ゲスト外の遮断」と同じものとして省略しない。
[AUP](https://cloud.google.com/terms/aup)は利用者による不正アクセス/妨害等にも適用される。
隔離の侵入試験は試験対象と許可範囲を絞り、provider基盤や他契約者への攻撃を行わない。

## Azure for Students

[offer公式](https://azure.microsoft.com/en-us/pricing/offers/ms-azr-0170p/)は$100/12ヶ月、18歳以上・対象高等教育機関の在籍等を条件とする。
高校生を含む「学校メールがある人全員」の対象ではない。1人1subscription、譲渡/自由合算不可、
credit枯渇時のsubscription無効化を考慮する。$100は毎月ではなく期間全体の総額。

- 無料/学生枠で今回の4人・保存量・必要nested SKU quotaが揃う根拠はなく、**無料での構成成立は不可/未確認**。
- [Dv4等の仕様](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dv4-series)にnested対応があっても、
  Linux KVM/libvirtのサポート保証とは読み替えない。[Hyper-Vの公式制限](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/nested-virtualization)は
  non-Microsoft仮想化の非サポートを説明している。今回のKVM方式は**未確認・採用不可**とする。
- rootless/runscは有料Linux VMで技術候補だが、quota/cgroup/VM kernel/ネットワークを実測していない。
  直接VMも有料候補で、管理disk・metadata・IAM最小権限・枯渇時復旧を別検証する。
- [教育FAQ](https://learn.microsoft.com/en-us/azure/education-hub/faq)の教育/非商用研究/開発試験という目的条件と
  [適用Online Services条項](https://www.microsoft.com/licensing/terms/en-US/product/ForOnlineServices)を確認する。
  教員運営の多人数サービスや将来の一般公開への適用可否は、契約主体/利用形態を示して確認が必要。
  学生の個人資格情報やsubscriptionを共有管理する構成にはしない。

Azure Linux SKUの現在単価は公開Retail Prices API/価格画面から今回取得できず、
第三者価格を公式確定値として転載しない。月額総額は**見積未確定**であり、最安候補の決定根拠にはしない。

## 月額の比較：CPUだけで無料と判定しない

税/為替/region/契約は未確定。1 USD=150円、1 EUR=170円は比較上の仮定で現行為替ではない。
730h/月の常時運用と、授業等80h/月の計算を分ける。停止中もP=40の保存とbackupを計上する。
80h案は**有料標準VMの計算資源課金が停止時に止まる条件を契約/shapeで確認した場合だけ**成立する。
無料枠控除・trial/学生credit・予約割引・spotは控除しない。回収/中断で唯一の編集データを危険にしない。

### 単価の根拠と計算入力

- OCI A1は[公式Compute](https://www.oracle.com/cloud/compute/arm/)の$0.01/OCPU h＋$0.0015/GB hを使用。
  Arm OCPUはx86の2スレッド換算と同一視しない。比較hostは12 OCPU/32GB、$0.168/h。
- [2026年11月1日付OCI公式価格表](https://www.oracle.com/asean/a/ocom/docs/corporate/pricing/oracle-paas-and-iaas-global-price-list.pdf)
  のblock capacity $0.0255/GB月＋performance $0.0017/VPU GB月から、10VPUの比較単価は$0.0425/GB月。
  2TiB以上を確実に予算化するため**2200課金GB**で$93.50/月を見込む（実容量単位を調達時照合）。
  独立bootは50GBで$2.125/月。低価格storage tierに変えてIO要件を満たしたことにはしない。
- Google N2-standard-16（16 vCPU/64GiB）は[公式価格表の掲載USD例](https://cloud.google.com/products/compute/pricing/general-purpose?hl=en)
  $0.776944/h。実region/SKUの見積りは未取得。disk価格は[公式の別課金項目](https://cloud.google.com/compute/disks-image-pricing)を参照し、
  対応SSD/balancedのregion単価が今回取得できなかったため、2TiBの**仮定予算$200〜350/月**で感度を示す。
  この幅は公式見積りではない。安いHDDだけでIO試験に合格すると仮定しない。
- 外部backupは既存試算と同じ3040GiBで、[B2公式](https://www.backblaze.com/cloud-storage/pricing)の保存単価を使った約$22.69/月。
  app/API・分離gateway/管理系は共通の**仮定$40/月**を加算する。実際のregion/サービス決定後に置換する。
  この額で実サービスの分離・可用性を確認済みではない。Firebase/メール/通信/IPv4/税/監視/工数は別見積り。

| 候補 | 実行系compute/月：80h / 730h | 停止中も保持する保存・backup等 | 合計概算/月（80h / 常時）、工数・税等別 | 採否の注意 |
| --- | ---: | --- | --- | --- |
| 月額専用ホストでKVM | 月額契約で停止しても同じ | 実効2TiB以上を含むhost賃料を**仮定€100〜150**、backup$22.69、共通$40 | **約2.64〜3.49万円 / 同額** | 実見積り未取得。SSD増設/初期費/回線は別で、条件次第で範囲を超える。現基準の優先候補 |
| OCI有料A1 host＋runsc | $13.44 / $122.64 | 2200GB $93.50＋boot$2.125＋backup$22.69＋共通$40 | **$171.76 / $280.96（約2.58 / 4.21万円）** | 価格上は安いが現VM基準との同等性未認定。無料枠で4人可とはしない |
| GCP対応L1＋KVM（B相当） | $62.16 / $567.17 | disk仮定$200〜350＋backup$22.69＋共通$40 | **約$325〜475 / $830〜980（約4.87〜7.12 / 12.45〜14.70万円）** | 対応SKU/地域の再見積り必須。trial quotaとnested性能が別条件 |
| microVM | 選択するKVM hostと同じ予算から開始 | KVMの2/4TiB計画を維持 | **レンタルKVMと同等予算＋導入/保守工数** | 軽量化だけで安くなると断定せず、CPU/IO実測後に再計算 |
| OCI等のUser別クラウドVM | A1 2 OCPU/4GB×4なら$8.32 / $75.92 | P=40のboot50GB＋別data50GB（最小block単位）を保持し、計4000GBで$170。backupもboot増量を再計算 | **下記の条件付き計算で約3.92 / 4.93万円** | metadata等未確認で現時点は採用不可。40台起動費を払う計算にはしないが、停止disk費は40組分 |
| Azure for Students/有料Linux | SKU現行公式単価未取得 | boot/data管理disk＋外部backup＋共通系を別計上 | **総額未確定。$100/年だけで恒常運用可能とはしない** | 対象資格・quota・SKU・KVMサポート・契約確認前は不採用 |
| 専用機の新規購入 | 初版A/B/Cの按分比較を保存 | 保存/外部backup/設置/電力/管理担当が必要 | **初版Aの工数別約1.53万円/月＋初期資金19万円等** | 学校に余剰設備なし。設置条件と初期費のため最優先にしない |

直接OCI VMの条件付き算数：boot最大使用50＋data最大20を各40組2世代で保全するなら、
backupは(50+20)×40×2+32+128=5760GiB、$6.95/十進TB月で約$42.99/月。
compute＋$170＋$42.99＋共通$40は約$261.31/$328.91（約3.92/4.93万円）。
[block作成仕様](https://docs.oracle.com/en-us/iaas/Content/Block/Tasks/creatingavolume.htm)の最小50GBを考慮し、
data20GiBを20課金GBのdiskとして見積らない。boot/dataとも50GB確保、backupのbootは50GiBと安全側に見込む。
最低boot47/50GBという公式記載差も安全側50GBで扱い、実容量と課金単位は見積り時に照合する。
これはKVMの3040GiB backup計画を小さく変更するものではなく、別方式で増える分の感度計算である。

月額専用hostは[Hetznerの仮想化network案内](https://docs.hetzner.com/robot/dedicated-server/virtualization/general/)等から技術候補になる。
ただし[AX42公式仕様](https://www.hetzner.com/dedicated-rootserver/ax42/)の2×512GBでは容量不足、現価格数値も取得できなかったため、
上の€100〜150をその商品の実売価格として引用しない。必要なSSD/CPUの実見積りが予算を超えれば不成立である。
高額購入案だけを外して、条件未確認の廉価serverを確定推奨することもしない。

## 利用規約・学校運用の確認事項

任意コードを実行できることは、他人への侵入・malware配布・scan・採掘・spamを許可することではない。
各providerのAUP、abuse対応、学生支援の教育目的/契約主体/年齢/譲渡制約、保存国・個人情報を学校と確認する。
Googleの[AUP](https://cloud.google.com/terms/aup)、Azureの[offer](https://azure.microsoft.com/en-us/pricing/offers/ms-azr-0170p/)と
[Online Services条件](https://www.microsoft.com/licensing/terms/en-US/product/ForOnlineServices)、
Oracleの[対象地域契約](https://www.oracle.com/contracts/cloud-services/)、
レンタルの[Hetzner規約](https://www.hetzner.com/legal/terms-and-conditions/)を契約前に再確認する。
本調査は特定契約における多人数サービスの利用許可を取得したものではない。

無料枠維持のために無意味なCPU負荷を発生させたり、複数アカウント/creditを無断集約したりしない。
「予算通知」はhard capではないため、quota/予約/新規起動拒否と費用監視、承認後の停止運用を別に準備する。
契約/credit失効前に既存データを消す運用は採らず、別の復旧可能なbackupと継続費用を確保する。

## 安全性試験の追加条件（全件NOT_RUN）

初版[検証計画](validation.md)の条件を全方式で維持し、以下を追加する。

| 対象 | 追加確認・合格条件 |
| --- | --- |
| rootless/runsc | daemon/runtime/sandboxの実UID、別User/Projectごとのuser namespace/cgroup、mount全列挙、Gofer/host process、network/metadata/socket拒否を実測。runsc停止時にrunc等へfallbackしない |
| microVM | /dev/kvm利用条件、jailer相当制限、guest kernel/rootfsの署名/更新、block deviceの一意RW、TAP/外側ACL、crash/job復旧を検証 |
| 直接cloud VM | provider IAM/metadata完全拒否、guest外egress、public IPなし、disk owner/再attach、削除保護、API応答消失とgeneration/fencingを確認。API鍵をゲスト/gatewayへ渡さない |
| 無料/学生枠 | 対象校/年齢/用途・quota・credit期限/停止/保持を確認。資源不足で同時枠や隔離を弱めない。学校参加と各Team/Project権限は別途確認 |
| ARM | Python/Node/GCC/code-server/必要拡張のARM64動作、Sentry syscall/IO性能を測定。x86のみの依存物があればその授業では不合格 |
| 費用 | 登録P=40の停止disk/backupも含む積算、80/730h、保存/転送/IPv4/ログ/為替/税/当番を照合。初回検証P=4と全員保存P=40を別に報告 |

現在のVM manager/gatewayのレジストリ・認可・fence実装は変更していない。
単にVMRoute.safe_readyをtrueと申告して別方式を実行しない。
方式変更を実装する場合は、その必要な契約変更と影響を示す別PR/承認を先に行う。

## 推奨の確定手順

1. 購入は保留し、学校/教員の利用資格と、GCP教育/試用枠で対応KVM SKUを短期検証できるかを確認する。
   対応quotaが得られなければ無断で課金化しない。1〜4合成環境の検証費用はP=40恒常運用とは別枠。
2. 同じ2/4TiB・4同時・隔離条件でKVM月額レンタルの見積りを比較し、初期費/設置不要という利点を評価する。
   例の月2.6〜3.5万円は予算比較であり、stock/価格確認後に最安適合候補を決める。
3. rootless/runscは低費用候補として残すが、現行の専用guest kernel要件との差を埋める安全性判断を省略しない。
   同等性を認定できなければ不採用。microVM/直接cloud VMも各未確認条件のまま採用しない。
4. 全員分の保存を満たす継続費・backup・失効/容量停止の実機合格が揃った後だけ、学校試験導入を別承認する。

**現在確定できるのは「無料だけで全要件を満たす根拠なし」「高額購入を先にしない」「安全基準は維持」の3点。**
調達可能な最安適合構成の確定は実見積りと実機合格待ちであり、本追記では実行ゲートを解除しない。
