# 安全性検証：合格条件・停止条件

## 判定の原則

**実基盤は未準備。以下23項目はすべてNOT_RUNであり、合格済みではない。**
この表は将来の専用ステージング基盤のための試験計画。
既存Docker/VM/DB/共有Git/個人領域を試験対象にしない。
実機試験は専用の合成User A/B、別Project、ダミーDB/共有Git宛てで行い、
本番への接続・走査・認証情報利用は所有者の別承認なしに行わない。

証跡にはOS/runtime/ビルドの版、対象Bindingと世代、時刻、操作ID、手順、期待値/観測値、
検証担当・別レビュアーを記録する。秘密を除いた証跡は組織の保管先で管理し、
JSONのevidence_idにはTEST-から始まる非機密の識別子だけを記載する。
JSONへPASSを記入できることは証跡の真正性や実機合格の検証ではない。

## チェックリスト

| ID | 将来の検証方法 | 合格条件 | 停止条件 |
| --- | --- | --- | --- |
| dedicated_host | 新資産ID、実ホスト、アプリ配置、管理者の照合 | アプリ/DB/既存Dockerから独立 | 接続先や既存媒体か不明 |
| kvm_available | 新Linuxのcapabilities、実VMでのKVM実行確認 | 専用カーネルのVMがハードウェア支援で動作 | KVMなし、未確認のnested、TCG等への黙った切替 |
| hypervisor_confinement | QEMUのUID/LSM/cgroup/デバイス・パスの実測 | VM別制限、任意パススルーなし | 無制限root/LSM無効・任意引数 |
| vm_binding | A/B/別Projectの操作・URL/ID差替え、衝突、世代変更 | 本人のVM/disk/設定のみ一致 | 名前だけで採用・他人へ切替 |
| storage_boundary | A端末からB/別Project/host共有を確認、共有Gitのダミーmain比較 | 未マウント・到達不可、他領域不変 | 他領域が読める/書ける・同一disk二重RW |
| ipv4_ipv6_egress | A/B間、管理網、ホスト、IPv4/IPv6/L2偽装を合成対象で試験 | ゲスト外のACLで新規通信拒否 | guest firewallのみ、迂回/IPv6抜け道 |
| metadata_block | metadata/host resolver等への到達と監視 | 意図しない経路/応答なし | メタデータ・管理情報を取得可能 |
| no_secrets_sockets | guestのmount/env/config/socket/機器を棚卸し | DB/Firebase/他人の鍵、Docker/libvirt socket、host deviceなし | 秘密や管理APIへ到達可能 |
| gateway_only | VM宛て直接HTTP/WS/SSHを外部・別VMから試験 | 公開はgateway 443だけ、guest入口は専用gateway IDのみ | VM直通、未知ホストfallback |
| dns_tls | 専用試験DNS、SAN/chain/SNI、有効期限、更新失敗・失効 | 正確なホスト/TLS、旧証明書は拒否 | 本番と衝突、信頼検証省略、平文迂回 |
| firebase_revocation | 実試験アカウントの失効・無効化、所属削除、Project移動 | 新規要求は拒否、許可は本人/現在世代のみ | クライアントID信用、失効を無期限キャッシュ |
| host_cookie_binding | A CookieでB/別Project/別Host、余分なDomain/上流Set-Cookieを試験 | Secure/HttpOnly/Path/Host束縛、上流へ資格情報を渡さない | Cookie共有、資格情報反射/転送 |
| csrf_origin | 他Originの交換POST/WS、code/verifier再使用、任意redirect | 正確なOriginとPKCE相当、一回消費、redirect固定 | 再利用・転用・任意転送可能 |
| ws_upgrade | 未認証/別Host/別Cookie/OriginでUpgrade | accept前に拒否。正規だけでcode-server動作 | 先にaccept、Upgradeだけ認証省略 |
| ws_revocation | WS/長時間HTTP接続中の所属削除・失効・期限・authorizer障害を時計で測定 | 最後の認可から15秒以内に切断、絶対5分以内 | 接続放置、障害時延長 |
| worker_mtls_nonce | 偽相手、期限切れ、nonce再利用、操作/Binding/世代改ざん | 拒否し副作用なし。証明書の役割を分離 | 任意XML/exec/削除/古い許可を受理 |
| lifecycle_fencing | 同時ensure、作成応答消失、lease切れ、旧manager復帰、停止失敗 | 同じ操作の照合、一意VM、状態不明は隔離 | 重複VM/共有disk、停止成功と偽る |
| data_retention | 編集→正常停止→再起動→新rootへ再作成 | 自分の編集/設定が保持、同じ本人disk | 空diskへ切替・削除・古いcopyで上書き |
| submission_snapshot | 新保存先から提出、symlink/hardlink/特殊ファイル、巨大/改変streamを試験 | 不変snapshot、危険入力拒否、既存承認/統合を維持 | ホストで無検証disk mount、guest.gitの実行、二重保存先 |
| backup_restore | 保全後に新隔離先へ復元しハッシュ/世代/二重起動を検証 | 所有者/内容が一致、旧世代は接続不可 | 復元未検証、旧VM同時RW、上書き復旧 |
| resource_quotas | 1→2→4 VMのCPU/RAM/IO/PID/容量/WS負荷を測定 | 上限実効、他VM・管理系が維持される | OOM/容量枯渇で他人へ波及、上限未確定 |
| logs_no_secrets | 正常/障害/検証エラー/WS/例外ログの合成secret検査 | token/Cookie/code/鍵/本文/内部接続先を反射しない | 生SDKエラー、秘密を含むアクセスログ |
| legacy_regression | 凍結・保存先切替後のチーム/Project/初期化/提出/レビュー/統合/復旧 | 元データ保持、旧APIと履歴の互換性 | 一括権限変更・既存ジャーナル/参照の破壊 |

**一つでもFAIL・NOT_RUN・不明があれば公開/実行ゲート変更を停止する。**
既存データへ影響し得る操作、対象資産の不一致、証跡不足、復旧未確認も即停止条件。
全項目PASSの申告後も、別レビュアーの証跡確認と別PRが必要で、自動許可は発行しない。

## 静的検査ツールの意味

tools/editor_foundation_check.pyは、明示したJSONと第7段階ソース3ファイルだけを読む。
Docker/libvirt/SSH、DNS、DB、SDK、HTTPへ問い合わせず、ファイルも書かない。
入力サイズ64 KiB上限、重複キー・NaN/Infinity・未知フィールド・型違いを拒否する。
root/host共有/秘密・任意経路・共有Cookie、実ドメイン/実IP、起動許可、予算の過剰割当て等を検出する。
ディスクは同時起動数でなく、停止中を含むmax_retainedで計算する。
エラーには入力値や内部パスを再表示しない。

第7段階editor_contracts.py/editor_api.py/editor_access.pyを、5758a62時点の
改行正規化SHA-256と照合する。これはソース変更防止の回帰であり、稼働バイナリ・VMの
完全性保証ではない。将来の正当なコード変更も止めるので、基準更新自体を別レビューにする。

設計例はVM用に固定し、既存Docker事前判定の不足を自動で無視するものではない。
業務チャネルの一致はルーターやファイアウォールの実測ではなく、CPU等の計算もベンチマークではない。
証跡は申告の形式だけを確認する。全件PASSと書いても実機確認/起動/配備許可の三値はfalseのまま。

| 出力 | 解釈 |
| --- | --- |
| design_consistent / 終了0 | 例の形式と宣言が整合。未実施でもこの結果になる。起動/配備の合格ではない |
| evidence_failed / 終了2 | FAIL申告がある。停止 |
| invalid_design / 終了2 | 入力・予算・第7段階保護ソース等が不整合。停止 |

## 今回のテスト方針

新規18件は一時JSON/ソースコピー・モックだけを使う。全PASS申告でも許可しないこと、
guest→DB/管理/他VM経路、秘密・Cookie条件、過剰割当て、型/重複キー、ゲート変更等を検査する。
第7段階の実行ゲート・API・認証ソース、既存Compose/DB/保存処理は変更していない。

既存回帰はWindowsの一時SQLiteとFirebase/Docker応答のモックを利用する。
PROJECT_TEST_DATABASE_URLとEDITOR_LAB_IMAGE_IDを空にし、実DB・実診断試験は起動しない。
実外部コマンドは既存Compose試験の[config](https://docs.docker.com/reference/cli/docker/compose/config/)（空の一時envと合成値による解析）だけに制限して実行した。
Docker engineの操作・照会、コンテナ起動、VM作成、証明書取得、DNS変更は行わない。
Linux/PG/実診断の検証を今回成功したかのように報告しない。

初回の補助ガードでCompose引数数を8と誤指定し、その静的検査1件だけを拒否した。
正しい7へ直して再実行し、全件結果を確認した。リポジトリのCompose設定には変更を加えていない。

実基盤チェック23項目のNOT_RUNは、このWindows回帰成功によってPASSへ変更しない。

### 実行結果（2026年11月17日）

- 新規の静的検証18件：18件成功。全PASS申告でも起動/配備許可がfalseであることも確認。
- Windowsの全件回帰248件：86件成功・162件スキップ。新規18件はこの248件にも含まれる。
- スキップはLinux/PG専用と明示実行の診断試験。今回はLinux/PG/コンテナ/VMを起動して再確認していない。
- 既存の認証・所属削除・常時無効API・WS拒否・診断失敗のモック試験も成功。
- Composeは空の一時envと合成値によるconfig解析のみ。Dockerデーモンへは接続していない。
- 既定CLIはdesign_consistent、NOT_RUN=23、三つの実行/配備/実機確認値はすべてfalse。
- 第7段階ソース3ファイルは正規化ハッシュ一致。既存TestClientの非推奨警告は残る。
- 文書のローカルリンク、23項目のIDと証跡JSONの対応も静的に確認した。
