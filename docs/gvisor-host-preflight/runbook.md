# 専用hostでの実機検証を始めるための準備・試験契約

**今回は全項目NOT_RUN。以下は将来の計画で、操作を実行するコードや自動インストール手順は含まない。** 既存のKVM/gVisor/認証/保存先を変更せず、実行禁止ゲートも維持する。

## 1. 用意するもの

担当教員・運用者は、専用hostの管理者、対象account、匿名のrun_id、期間、作業範囲、費用上限、停止連絡先を決める。学校の利用承認、利用規約と任意コード実行の可否も確認する。本番DB・共有Git・workspace・Docker Desktopを試験環境として使わない。

準備する専用品は、保守中のLinux ARM64またはx86_64、非rootな管理chain、User×Projectごとのhost principal/subuid/subgid、local quota対応FS、検証用空volume、合成ファイルとコード、隔離されたnetwork、認証リレーの試験経路、独立したbackup先である。本体Web/API/DBと別hostにする。管理SSH鍵・クラウド権限・Docker socket・Firebase秘密鍵はsandboxへ渡さない。

採用するrootless engine、runsc release、OS/kernel、systemdを固定し、互換性をレビューする。[gVisor install](https://gvisor.dev/docs/user_guide/install/)は現在Linux 5.6+・ARM64/x86_64を対象とするが、最低番号だけで合格にしない。配布bundleにはsidecarが含まれる場合があり、runsc単体のhashでは全体を検証できない。署名/公式配布checksum・release・全componentと配置を記録する。公式packageの導入がDocker設定へ作用する可能性もあるので、既存hostへそのまま導入する案は禁止する。

## 2. 二つのゲート

静的調査だけで強制の成立は証明できない。そのため「限定診断」と「エディタ試験」の開始条件を分ける。

1. **限定診断の前提**：担当者が専用host、合成データ、資源上限、network遮断の外側境界、backup、監査、緊急時の安全な停止方針をレビューする。事前checkerにFAILがあれば停止。不明は未解決として列挙し、それを調べる限定試験内容だけを別途承認する。今回はこの診断も実装・実行しない。
2. **エディタ実機試験の前提**：以下の必須隔離条件について限定診断の実証跡がすべてPASSであること。UNKNOWN/NOT_RUN/FAILが一つでもあればcode-serverを起動しない。管理者レビューは別途必要で、JSONを書き換えるだけで許可する仕組みは作らない。

必須条件は、runsc以外へのフォールバックなし、全chainのrootless、非root guest、controllerとdisk quotaの実強制、本人領域限定、host秘密情報不可、network遮断、個人データ保存/復元、障害時のfail-closedである。gatewayを含むブラウザ試験は、さらに本人認証・現在所属・generation・失効・HTTP/WebSocket制御の条件を満たしてから進める。

## 3. 安全なimage buildの準備

PR #15の`artifacts/gvisor-editor/`を維持する。default entrypointはexit 78であり、現段階のimageをbuildしてもcode-serverは起動しない。この制限を外す修正は別PRでレビューする。

将来のbuildは、本番や利用者コードを置かない専用builderで行う計画にする。公式code-serverのarchitecture別digestをpinし、package version、対応Node LTS、Python/GCC/Git、取得元、SBOM、脆弱性検査結果を保存する。builderの一時的なpackage取得通信とsandboxの遮断networkを分ける。repo rootをbuild contextに渡さず、secret/build credentialをimageに残さない。ARMとx86は別に検査し、emulationでbuildできたことを実機互換性の代わりにしない。

実行構成は非root1000:1000、capability全削除、no-new-privileges、read-only root、本人volumeとサイズ制限tmpfsのみ書込み可。setuid helperがhostに必要なことと、guestからsetuidを削除することは別条件。runsc/systrapを明示し、engine・runtime・最終inspectで一致を確認する。rootless built-inモードへ切替えてcgroup不具合を回避しない。

## 4. 1人分の資源・保存・権限契約

最初の比較候補は1 vCPU・2GiB、従来基準2 vCPU・4GiBも残す。前者の性能は未確認。root予約16GiB、本人data20GiB、pids256、swap追加不可、tmpfs合計96MiBを上限予算内へ計上する。host用に少なくとも1CPU枠・2GiBとdisk/inodeの余裕を仮置きし、実測で増やす。小さくして無料成立と断定しない。

volumeはUser×Projectと一意に結び付け、専用resolverで所有者・inode・symlink/hardlink・quotaを検査する。本人dataは停止後も保持し、コンテナと別の寿命で管理する。停止中のdisk予約も残す。共有Gitや既存workspaceをmount・コピー・chownしない。変更提出フローとの安全なimport/exportは今後の別設計で、今回追加しない。

## 5. 実機測定と証跡

各試験にrun_id、対象ownerの匿名ID、Project、generation、operation_id、採用release/image/bundle digest、開始/終了、測定器、raw証跡の安全な保存先、判定者を記録する。token/鍵/パスワードや本人ファイル内容をレポートへ含めない。[未実施template](evidence.example.json)は管理者記録用で、checkerへ読ませる許可設定ではない。

|試験|具体的な測定方法（将来実施）|PASSの条件案・停止条件|
|---|---|---|
|runtime/version/bundle|承認済みrelease一式とchecksumを照合し、専用境界でversion/起動を検査|全component一致、systrap、runc不在時も代替起動なし。相違・不明なら停止|
|rootless全chain|host側からdaemon/runtime/Sentry/Gofer/sidecarのUID、userns、mapping、guest1000を照合|管理chainがrootfulでなく、本人ごとmappingが分離。単なるdockerグループは不可|
|CPU|専用の有限時間の合成負荷をかけ、cpu.max・cpu.stat・host余裕を同時測定|指定期間のquota内。計測許容幅を試験前に決定し、無制限なら停止|
|RAM/swap|上限前後へ段階的に合成allocationし、memory.max/events、swap.max、host残量を検査|sandbox内で上限制御。host全体OOMなし。書込み中断データの保護も確認|
|pids|上限近くまで有限数のworkerを作りpids.max/eventsを測定|256以下に制御。fork bombは禁止。hostプロセスへ影響したら停止|
|disk/inode|専用quota領域だけに段階的な合成dataを書き、quotaと空き予約を測定|data20GiBとroot枠を超えない。host空間/inode安全余裕前に新規受付停止|
|本人・別Project|別principal/volumeに置いたcanaryへguestの通常I/O、symlink/hardlinkでアクセスを試みる|本人のみ可。別者/別Project/host root/秘密相当canaryへの読書き不可|
|network|外側FW記録と専用sinkでIPv4/IPv6・DNS・loopback・Unix abstract socket・相互通信を検査|認可済みリレー以外不可。管理網/DB/Git/metadata相当先へ一つでも到達すれば停止|
|toolchain|合成Python実行、CのGCC compile/run、Node小プログラム、Git local commitを実行|非root・遮断network内で成功、外部取得不要。各architectureで別判定|
|保存・再開|合成dataのchecksum/ownerを記録し、flush/正常停止/再作成後を比較|同じ本人volumeとdata。旧generationで操作/接続不可|
|異常・復旧|応答消失、DB相当台帳不通、worker終了、保存失敗を制御されたfaultで再現|UNKNOWNの枠保持、二重作成なし、data削除なし、古いfence拒否|
|backup/restore|独立したbackupを暗号化・検査し、新しい試験volumeへ復元|checksum/owner一致、RPO/RTOを測定。例：RPO24h/RTO4hを学校と合意し検証|
|ブラウザ|専用試験経路で本人のみ接続、取消し、Host/Origin、WebSocketを検査|既存gateway契約（最大15秒再認可等）を満たす。URLだけで接続不可|

安全余裕は予約済み未使用領域・thin metadata・inode・backup stagingを含める。計画値として物理空き20%以上を残し、予約後に下回る新規受付を拒否する。FS/quota観測不能でも拒否。既存4枠は維持し、容量不足なら1台目から拒否する。測定負荷の上限・監視・停止担当者を決めずに負荷試験しない。

実providerのmetadataや本番DBへ実際にアクセスを試みない。隔離network内の専用sinkでdenyを確認し、provider固有のmetadata/IP/routeについては担当者の承認範囲内で追加検証する。ネットワークnoneでブラウザが繋がらないからとhost networkを使う変更は禁止。現在はリレー・公開経路未実装である。

## 6. 障害・後片付け

起動前のjournalへ予定sandbox/volume IDとowner/generation/fenceを保存し、作成応答が消えた場合も別環境を作らない。記録済みimmutable IDと実resourceの所有者・volume・設定hashを読取り照合する。名前やラベルだけでは削除対象にしない。不明なら隔離して枠を保持し、管理者へ確認する。

保存失敗・停止失敗で強制停止しない。ジョブ静止・全client保存・syncの証拠を得るまで、停止成功や枠解放を記録しない。異常終了時は元dataを保持し、復旧先は新しい検証volumeとする。元データを復元処理で上書きしない。

今回、resourceを作るコードも消すコードも追加しない。将来の後片付けは専用品の正確なID、正常停止、backup、担当者承認が揃った診断resourceだけを対象とし、data保持と実行resourceの除去を別扱いにする。prune、down -v、volume削除、強制停止、既存volumeの権限一括変更を含めない。
