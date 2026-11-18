# 第14段階：専用Linuxホストの読取り専用事前調査

基点はPR #15マージ済みmain `c980886`。今回は**準備コード・モック試験・手順だけ**を追加する。専用Linux実機は使っておらず、実機検証結果はすべてNOT_RUN。KVM、gVisor参照アダプター、Firebase、gateway、実行禁止ゲートを変更しない。

## チェックできる範囲

`backend/gvisor_host_preflight/`は標準ライブラリだけを使う独立ツール。DB、アプリ、Docker/libvirt SDK、socket、subprocess、クラウドAPIをimportしない。ファイル作成・設定変更・namespace作成・quota設定・サービス操作・外部コマンド実行はしない。runscの`--version`も実行しない。

|対象|読取りで得る情報|得られない情報|
|---|---|---|
|OS/CPU/kernel|uname（hostname除外）、os-releaseのID/VERSION_ID、ARM64/x86_64|採用releaseとの動作互換性|
|rootless|調査プロセスの非root UID、namespace link、sysctl、subuid/subgid・helper属性|全process chain、UID mappingの実効性、NSS/全directoryとのID衝突、LSM許可|
|cgroup|standard cgroup2 mount、cpu/memory/pids controller、現在所属の制限値|委譲可否・実書込み権限・sandbox制限の実強制|
|runsc|ELF64 architecture、実行bit、SHA256、任意の期待digestとの一致|真正なrelease、version、sidecar含むbundle完全性、systrap実行可否|
|disk|指定local FSの空きbytes/inodes、mount上のquota option|本人quota、thin空間予約、実上限、保存/復元|
|network|現在net namespace link、max_net_namespaces|unshare許可、hostと別namespace、egress/metadata遮断|

subuid/subgidは本人に連続65,536以上を持つことと、ファイル内の全range重複を保守的に検査する。ユーザー名・range・passwd内容は出力しない。欠測・NSS利用者・複雑なmount/権限不足はUNKNOWN。newuidmap/newgidmapのhost側setuidは既存設計の限定例外で、コンテナ内rootを許可する意味ではない。

## 判定の意味

- `OBSERVED`：静的な値を読めた。**PASSや実行許可ではない**。
- `FAIL`：調査時点で必須候補に反する値（root、無効userns、range不足/重複、controller不足、architecture/hash不一致、空容量ゼロ等）。自動修正しない。
- `UNKNOWN`：欠測、解析不可、未対応、実効権限不明。良い値を補わない。
- `NOT_RUN`：起動/強制/保存/通信試験を行っていない。モック成功で置き換えない。
- `PASS`：将来、[実機手順](runbook.md)の証拠と管理者レビューで個別項目を合格とした場合だけの用語。本ツールは発行しない。

レポートの`readiness=BLOCKED`、`launch_enabled=false`、`deployment_allowed=false`、`connectable=false`は固定。実行ゲート、`Capabilities`、`VMRoute`へ変換しない。終了値は常に2（実行許可なし）。調査の完了はJSONの`collection=COLLECTED`で区別する。個別UNKNOWN/FAILがあっても収集を継続するため、COLLECTEDも全項目観測済みという意味ではない。

## 使い方

既定起動ではホスト調査さえ行わない。Windowsで試せるのは以下の無調査モードとモックテストまで。

```powershell
# 新しいworktreeのbackendで、開発用Pythonを使用。exit 2は想定どおり。
python -B -m gvisor_host_preflight.check
python -B -m unittest discover -s tests -p test_gvisor_host_preflight.py -v
```

将来、担当者が許可した**専用Linux検証host**だけで、非rootの試験用accountとして次の読取り調査を行う。既存アプリhost/Docker Desktopには実行しない。パスとdigestは担当者が確認した値へ置換する。sudo不要で、不足権限を昇格して解決しない。

```bash
# Linuxの専用コピーのbackendで実行。bytecode cacheも作らないよう-Bを付ける。
python3 -B -m gvisor_host_preflight.check \
  --inspect-dedicated-host \
  --runsc-path /usr/local/bin/runsc \
  --storage-path /srv/editor-lab-data \
  --expected-runsc-sha256 <承認済みrunscファイルの64桁SHA256>
```

この例は今回実行していない。host・storage pathは既に用意された検証専用品に限り、ツールは作成しない。stdoutのみを返し、出力ファイル引数もない。未知CLI引数や依存例外に入力値を反射しない。`--run`、`--install`、`--fix`等は存在しない。

## 読取りの安全性と限界

親directoryはFD経由のO_NOFOLLOWで辿り、最終ファイルもsymlink・特殊ファイルを拒否する。通常ファイル/proc/sysfsをサイズ上限付きで読み、hardlinkを拒否する。runsc候補は256MiB以内で読取り前後の変更を確認する。ELFやhash一致はバージョン証明ではない。namespaceのproc magic linkは既知の自プロセスに対するreadlinkだけで、setnsしない。

runsc/UID helper/保存先はmountinfoを先に調べ、ext4/xfs/btrfs以外（NFS/autofs/FUSE/overlay等）は未対応としてUNKNOWNにする。symlinkやmount escapeを誤解しないため、複雑なmountinfoでは保守的に拒否する。`/usr/lib/os-release`を優先し、`/etc/os-release`がsymlinkの場合もsourceしない。

前提は管理者が管理する信頼できる専用hostとlocalな`/etc`/`/usr/lib`/proc/sysfs。悪意あるhost管理者、同時mount差替え、カーネル侵害を防ぐツールではない。ファイル読取りに伴うOS側のatime/cacheまで不変にするフォレンジックツールでもない。直接ネットワークI/Oは行わず、NSS照会・process列挙・環境変数・鍵・`.env`・利用者ファイルを読む機能はない。

既存保護hashはPR #15のcheckerをそのまま回帰試験で利用し、変更しない。最新の結果とNOT_RUN一覧は[結果](results.md)、次の準備は[実機手順](runbook.md)、課金/無料枠は[OCIチェックリスト](cloud-checklist.md)を参照。
