# 将来の構築・停止・復旧手順

## 最初に読むこと

**本PRでは手順Aだけを実施する。B以降は未実施の将来手順。**
構成は[architecture.md](architecture.md)、合否は[verification.md](verification.md)を参照する。
新規ホストの名前/IP/管理担当者、保存用媒体、ネットワーク、独立ドメインはまだ未確定。
値を推測して既存Windows/Docker Desktop/WSL、既存DB・ディスクへ作業してはいけない。
このリポジトリにTerraform、cloud-init、VM作成スクリプト、適用用libvirt XML、
DNS登録/証明書取得、実エディタ起動の自動化は追加しない。

## A. 今できる静的確認（Windows PowerShell）

PRの作業ツリーにあるbackendへ移動する。以下はファイルを読むだけで、Docker/SSH/DBへ接続しない。
既存のPython仮想環境を使い、この作業だけのためにパッケージやOS設定を変更しない。

```powershell
Set-Location <第8段階の作業ツリー>\backend
python tools/editor_foundation_check.py
python -m unittest discover -s tests -p test_editor_foundation.py -v
```

pythonが見つからない場合は既存venvのpython.exeの絶対パスを使う。
リポジトリ直下から実行するなら`python backend/tools/editor_foundation_check.py`でもよい。
サンプルJSONは自動で読み、実接続先へ置換しない。実ドメイン/秘密/証明書パスは受理しない。
整合していればdesign_consistentとなるが、real_infrastructure_verified、deployment_allowed、
editor_launch_enabledはすべてfalse、実機チェック23件はNOT_RUNのまま。
0は静的形式の成功、2は形式不備・申告FAIL・ファイル/保護ソース不一致等である。
0を配備成功や起動許可と解釈しない。

診断のためにも既存editor_preflight（Docker info）やコンテナ試験を今回は実行しない。
GUIの共通code-server、既存ボリューム、実データの権限を変更しない。

## B. 専用基盤の準備票（まだ実施しない）

管理担当者と次を埋め、既存環境と違うことを確認してから構築作業の許可を得る。

| 項目 | 決める内容・停止条件 |
| --- | --- |
| 専用ホスト | 資産ID、設置先、管理者、OS更新責任者。既存アプリのホストなら停止 |
| 仮想化 | Linux KVM対応CPU、/dev/kvm、CPU脆弱性対策。クラウドならnested KVM対応と提供条件も確認。不可なら別ホストを選び、通常コンテナへ黙って変更しない |
| 資源 | 初期予算12 vCPU相当/32 GiB/512 GiB、保持環境数と同時枠、容量監視、暗号化バックアップの別媒体 |
| 管理網 | 管理者接続元、manager、hypervisor、gateway、authorizerの分離。実行VMに管理網NICを付けない |
| ドメイン | アプリと別のサイト、管理組織、DNS担当者、CA/更新/失効担当者。まだ取得・変更しない |
| 資格情報 | 管理者用・libvirt用・gateway用の別ID/鍵管理。VMテンプレート/Gitへ格納しない |
| 復旧 | データ保全、操作ジャーナル、復元試験担当、目標復旧時間と許容データ損失、メンテナンス窓 |

CPU/RAMはゲスト分だけでなくOS・QEMU・manager・gatewayの常駐分を含める。
停止中のデータも容量へ加える。SSDの公称容量を全て空き容量として使わない。
RTO/RPOはこの設計で保証しない。管理者が測定して合意できなければ本番化を止める。

## C. 新Linuxホストだけを構築する（将来の管理者作業）

1. コンソールで資産IDと新規ディスクを照合し、既存アプリのIP/媒体でないことを二人で確認する。
2. サポート中のLinux LTSを新しい専用媒体へ導入し、OS/CPU microcodeを更新する。
   KVM/QEMU/libvirt、sVirtまたはAppArmor、cgroup、監査/時刻同期を採用OSの公式手順で導入する。
   実際のバージョン、配布元、署名/チェックサム、更新日を記録し、未確認なら止める。
3. root直接ログインを避けた管理IDと許可接続元を用意する。管理者の鍵をゲストへ流用しない。
4. Windowsから接続する場合、まずコンソール等の別経路でSSHホスト鍵fingerprintを照合する。
   不一致をknown_hosts削除や自動承認で回避しない。

以下は**将来、確認済みの新ホストへ**使う読み取り例。今回実行しない。

```powershell
Get-Command ssh
ssh -o StrictHostKeyChecking=yes <管理ID>@<新規ホストの確認済み管理名>
```

接続先がLinuxであることを確認して、Linux側プロンプトで読み取る例：

```sh
uname -s
lscpu
test -c /dev/kvm
virsh -c qemu:///system capabilities
```

このvirshは既に導入・承認された新ホストの能力を見るだけ。出力をそのままGitへ登録しない。
KVM不在、LSM無効、管理資格情報不明、VMプロセスが無制限rootになる場合は停止。
既存Docker Desktopをrootless化したり、既存Dockerデーモンを停止したりして解決しない。

## D. 管理・ネットワーク境界を先に作る（将来）

1. 管理サービスとgatewayの場所を決め、FastAPIからlibvirt/SSHへ到達する経路を作らない。
2. libvirt管理は専用CA/相手IDと管理ACLで限定する。アプリのclient証明書をlibvirt管理者として受理しない。
3. managerはENSURE_STARTED/GET_STATE/STOPと固定テンプレートだけを受理する。
   アプリからのraw XML、ホストパス、イメージ、VM削除・disk削除の要求は拒否する。
4. ゲスト用ポート/仮想NICを分離し、guest発の新規接続をホスト/vSwitch/上位ACLで既定拒否する。
5. 別VM、ホスト管理IP、DB/共有Gitのネットワーク、Docker/libvirt管理機能、metadataへの遮断を
   合成データの専用拒否対象で測定する。稼働DBへログイン・ポート走査する試験を勝手に行わない。
6. IPv6/L2/偽MAC/偽送信元、DNS/NTP等の管理例外も検証する。未確認の例外をguestへ開けない。

静的チャネル表をそのままファイアウォールへ適用しない。
実際のIP/ルーティング/許可インターフェースを記録した別の適用定義とレビューが必要。

## E. 合成データ用VMテンプレート（将来、まだ起動しない）

管理担当者が次の値で新規VM定義を作り、レビューする。既存VMやディスクのパスを再利用しない。

| 項目 | 初期値/必要条件 |
| --- | --- |
| VM identity | レジストリのUser/Project UUIDとgeneration。表示名だけを認可にしない |
| CPU/RAM | 2 vCPU/4096 MiB、CPU時間・RAM・PID/IOの外側上限。VM間のメモリ重複排除等の共有最適化も脅威評価 |
| root | 新規16 GiB、署名/ハッシュ確認済みLinuxイメージ。アプリの秘密や既存dataを焼き込まない |
| data | 新規20 GiB、本人専用、停止/再作成で保持、複数VMへRW接続不可 |
| NIC | 本人VM用の隔離ポートのみ。public IP、host networking、NATでの無制限外向きは不可 |
| 機器 | ホストFS共有、9p/virtiofs、USB/PCIパススルー、nested KVMデバイス、管理ソケットなし |
| サービス | code-serverは非root UID、sudo不可。管理は別ID。今回サービスを導入・起動しない |
| 入口 | code-serverはloopbackだけ。gateway用mTLS入口と専用leaf IDを別途検証 |

ゲストサービスの将来検討値はNoNewPrivileges、空CapabilityBoundingSet、ProtectSystem=strict、
PrivateTmp/PrivateDevices等。本人data/stateと必要な一時領域だけを書込み許可にする。
Node、拡張、言語サーバー、コンパイラとの互換性を測り、動かないからと制限を一括解除しない。
ゲストrootへの侵入を想定するので、このサービス設定だけで外側ACLを代替しない。
rootディスク更新とユーザーデータは分離し、data内にgateway/Firebase/libvirtの秘密を置かない。

VM作成・起動はこのPRに含めない。定義確認後、別PRでmanagerと永続レジストリ/リースを実装する。

## F. DNS/TLS/認証入口の準備（将来）

1. DNS/証明書担当者が、既存アプリと異なるサイトの名前とgateway宛てだけのレコードを設計する。
2. 外向きIP・SAN・更新/失効方法・監視・CAAを確認し、組織の発行手順を別途承認する。
   example.com/example.net/192.0.2.10を実際に申請・接続しない。
3. ステージングCAまたは実ドメイン用CAを選定してからTLSを構成する。今回は鍵生成/証明書取得を行わない。
4. gatewayの入口でHost/SNI/Origin、Cookie、Firebase由来の所有関係、現在所属を照合する。
5. HTTP/WS双方を通すrelay、接続中再認可、絶対期限、失効配信、照会障害時の切断を実装する。
6. VM直アクセス、別ホストCookie、Cookieのupstream漏れ、上流Set-Cookie、SSRF/任意転送を試験する。

NGINXの単純な転送設定やUpgrade時だけの認証を、WSの継続失効の代わりにしない。
未知のVM/ホストにfallbackする経路は作らず、証明書不一致・authorizer障害は拒否する。

## G. 起動許可前の順序

1. manager/レジストリを実装する別PRで、作成意図の事前記録、同一操作IDの照合、世代制御を完成させる。
2. 保存先切替と限定スナップショット移送を実装・試験する。元領域を無条件に再コピーしない。
3. 合成A/B/別Projectで検証表を実行し、ログを秘匿化して外部の証跡保管先へ置く。
4. 新しい環境だけでバックアップ復元・作成応答消失・停止失敗・通信断を試験する。
5. 未実施/FAIL/不明を一つでも残したまま起動ゲートを変更しない。
6. 管理担当者と別レビュアーが証跡を確認し、実行ゲート変更を別PRで承認する。

初期版にデータ削除APIは設けない。停止・再起動をデータ削除と結びつけない。
本手順だけで第7段階の503やWS全拒否を解除する方法はない。

## H. 失敗・復旧時の作業順（将来）

| 異常 | 最初の対応 | 再試行/復旧の条件 |
| --- | --- | --- |
| 作成中断/応答不明 | 操作ID、予定VM/disk、世代を保存。新規接続を拒否 | 限定照会で実体・所有者・設定が一致した場合だけ同じ操作を続行。別VMを作り足さない |
| 名前/所有/ディスク不一致 | 全体を隔離し、保全して担当者へ通知 | 勝手に採用・上書き・削除しない。二人で照合してから個別復旧 |
| 停止失敗 | gateway許可失効、外側ネットワーク拒否、状態ERROR | 停止を確認するまでdiskを別VMへRW接続しない。強制電源断は個別承認とFS復旧確認が必要 |
| authorizer/manager通信断 | 新規操作拒否、WSは再認可期限で切断 | 最後の成功を無期限延長しない。復旧後も世代と現在所属を再確認 |
| ホスト故障 | 関係接続失効、ホストとdiskの排他を確認 | バックアップを新しい隔離先へ復元し、旧ホストの復帰で二重起動しないようfenceする |
| 個人データ破損 | 元diskを保全し、書込み停止 | 別diskへ復元して内容を検証。古いcopyを最新の個人領域へ自動上書きしない |
| 証明書/鍵の失効 | 新規接続を止め、既存接続も期限内に切断 | 役割別の鍵をローテーションし、旧世代を受理しないことを確認 |

復旧担当者はパスワード/秘密鍵/生トークンをログやGitへ貼らず、操作IDと秘匿化した結果で照合する。
既存DB/Git/workspaceとDockerボリュームは本基盤復旧の自動削除対象に含めない。
