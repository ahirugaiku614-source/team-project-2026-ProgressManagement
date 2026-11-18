# 第9段階：個人VM管理の参照実装と永続モック試験

## 到達点と保護範囲

2026年11月18日、PR #10をマージしたmain `deeca6a`を基点に実装した。
User×Project別のLinux VM（KVM/libvirt）と独立HTTPSゲートウェイという
[第8段階の設計](editor-foundation/architecture.md)を維持する。

今回のコードは`backend/vm_manager/`にあり、**FastAPIからimport・呼出し・公開していない**。
実ドライバーも接続先設定もない。既定ドライバーは常に拒否する。
`RUNNING`はテスト用の模擬状態であり、code-serverが動いたことを意味しない。
既存APIの503、WebSocket全拒否、実行無効ゲートと保護ハッシュは変更していない。

既存のDocker Compose、PostgreSQL、共有Git、個人workspace、移送処理、DBマイグレーション、
認証設定は変更していない。既存backendのbind mount/autoreloadを避けるため別worktreeで作業した。
試験は新規TemporaryDirectoryのSQLite、合成データ、専用Python子プロセスだけを使用する。
既存ボリューム、実VM、Docker Engine、SSH、libvirt、クラウドAPIには接続しない。

| 区分 | 内容 |
| --- | --- |
| 実装済み | 安定ID、所有関係検査、操作記録、世代・lease、枠予約、隔離・再照合、既定拒否 |
| モックで検証 | 起動/停止/状態観測、境界側fencing、応答消失、プロセス停止、複数プロセス競合 |
| 設計のみ・未実装 | 本番管理サービス、現在所属照会との通信、署名/mTLS、実VMアダプター、HTTPS接続・失効 |
| 未検証 | 実KVMの隔離、ゲスト外FW、実ディスク耐久性、ホスト障害・停電、実ネットワーク分断 |

## ファイルと設計の対応

| ファイル | 役割 |
| --- | --- |
| `backend/vm_manager/contracts.py` | 既存EditorBinding/WorkerCommandを再利用。固定VM・disk ID、fence、観測結果、限定ドライバー契約 |
| `backend/vm_manager/registry.py` | 専用一時SQLiteに所有者・状態・世代・操作を記録。枠計数と予約を同一トランザクションにする |
| `backend/vm_manager/manager.py` | 認可→操作準備→dispatch→完了の処理。応答不明時に隔離し、再送だけで作り直さない |
| `backend/tests/vm_manager_fakes.py` | 管理DBと別の永続モック。実VM・diskは行データで表現し、境界側の世代検査を再現 |
| `backend/tests/test_vm_manager.py` | 一時領域だけの単体・障害・spawnによる複数プロセス試験 |

## 永続化の位置付けと同時起動枠

SQLiteは**単一ホストでの試験用参照ストレージ**であり、本番の永続化方式の決定ではない。
既存PostgreSQLへ接続・DDL適用しない。新規作成はO_EXCLで既存ファイルを上書きしない。
再開は専用application_id/markerを確認し、通常ファイル以外とhardlinkを拒否する。
初期作成が途中失敗したファイルも自動削除・初期化せず、調査対象として残す。
管理者が用意した私有ローカルディレクトリが前提であり、親ディレクトリ置換や
特権ホスト攻撃者に対するパス隔離機構ではない。NFS/共有FS/複数ホストは対象外。

BEGIN IMMEDIATEで書込みを直列化し、予約対象を数える処理と操作意図の保存を同時に確定する。
メモリ内カウンターは使わず、別プロセス・再起動後も同じ専用DBを参照する。
SQLiteの[同時書込みとトランザクション](https://www.sqlite.org/lang_transaction.html)と
[ファイルロック](https://www.sqlite.org/lockingv3.html)に従う。同じファイルを共有する範囲の保証であり、
別DBを複製して起動すると全体上限を守れない。本番のHA/DB選択は次段階で確認・承認が必要。

上限は初期値4、参照実装では1〜4へ縮小可能。**全Projectを合計した管理レジストリ単位**の枠である。
PREPARING/STARTING/RUNNING/STOPPING/QUARANTINEDが枠を使用する。
ABSENT/STOPPEDのみ枠を使用しない。期限切れだけでVMが止まったと推測して枠を戻さない。
予約数の上限であり、不正な管理者操作でVMを追加した状況まで制御できるものではない。

1Project最大8人は利用規模の前提。今回チーム管理に8人制限を追加していない。
8人が順番に利用しても8個の所有関係とデータを保持できる一方、同時予約は4個まで。
同じUserでもProjectが違えば別VM・別diskとなる。
第8段階JSONのmax_retained=4は従来の容量例のまま維持した。
8環境を保持する場合、従来の予約64GiB＋バックアップ枠256GiB＋(root16＋data20)×8で
**最低608GiB**となり、従来の512GiB例では不足する。これは使用量予測の計算で、
実測、スナップショット増分、追加Project、バックアップ世代は別途必要。
実容量・予算・保持上限の承認までは配備できない。既存planの合格を8環境の容量保証に読み替えない。

## 所有者と限定インターフェース

VM IDは既存EditorBinding.environment_key、disk IDは別名前空間のUUID5から固定する。
IDは認可用の秘密ではない。リクエストで任意ID・パス・コマンド・イメージを指定させない。
操作はENSURE_STARTED、STOP、GET_STATEのみで、削除・強制停止・任意execはない。

管理ロジックはactorとbinding.user_idの一致、およびAuthorizerの現在許可を確認する。
DBのTeam/Project/User、VM/disk IDも一致を確認する。dispatch前と応答保存前にも認可を確認する。
認可失敗・照会例外は拒否し、実行後の失効は枠を隔離保持する。
テストのMembersはメンバー集合のモックで、Firebase認証の代替実装ではない。
将来は既存Firebase・現在所属・Project対応確認からのみbindingとactorを確定し、
署名付き短期許可/mTLS/nonce消費を管理境界で検証する。現在は通信・API接続を実装していない。

ドライバーもVMとdisk両方の所有者を**副作用前**に照合する必要がある。
一度確認したVM/diskが見つからない場合、新しい空環境へ黙って置換しない。
管理DBの既知フラグと境界のrequire_vm/require_diskを使って隔離する。

## 状態遷移と冪等性

```text
ABSENT --start予約--> PREPARING --dispatch--> STARTING --確認--> RUNNING
STOPPED --start予約--> STARTING --確認--> RUNNING
RUNNING --stop予約--> STOPPING --確認--> STOPPED
途中失敗/応答不明/期限切れ/所有者不一致 --> QUARANTINED（枠保持）
QUARANTINED --新世代GET_STATEで境界をfenceして観測--> RUNNING / STOPPED / ABSENT
```

同じoperation_idは同じbinding・操作だけで再送でき、既存記録を返す。
ACTIVEでも再dispatchしない。別operation_idは進行中処理を横取りできない。
RUNNINGへのstart、ABSENT/STOPPEDへのstopは新しい副作用を起こさない。
過去操作のresult_stateと現在environment.stateは別に返す。
過去startの成功を再取得しても、現在停止中のVMをRUNNINGと表示したり再起動したりしない。

新しい実行/照合はgenerationを増やし、operation_id・所有者・VM/disk・期限と束縛する。
leaseは絶対時刻ミリ秒、既定30秒、試験では1秒。延長APIは実装しない。
期限後は旧操作を成功として保存しない。完了時にもDB保存済み期限・操作・世代を照合する。
DBに保存した時刻より時計が戻れば拒否する。同時呼出しで古い採時値が後からロックを取っても
安全側に拒否し得るため、その場合は新operation_idで状態を再確認する。
時計の進み・ずれによる早期隔離は許容するが、実配備には両境界の時刻同期と許容誤差設計が必要。

## lease、fencingと不明状態の復旧

管理DBで世代を比較するだけでは、送信済みの古い要求が遅れてVMを作ることを防げない。
将来のアダプターは**副作用が起きる最終境界**で期限・最高世代・operation_idを永続検査し、
副作用と一体に実行保証しなければならない。GET_STATEは単純なstatus取得ではなく、
新世代を先に確定して旧要求を排除するfence-and-observeである。
未完了jobや観測の欠落をABSENT/STOPPEDと報告して枠を解放することは禁止する。
libvirt呼出しとの原子性、起動中jobの追跡、管理サービスの単一権威性は未実装であり、
実アダプターを差すだけで保証が完成するわけではない。

| 障害点 | 残る状態と再開方法 |
| --- | --- |
| 意図保存前のDB失敗 | ドライバー未呼出し。未確定の枠予約もロールバック |
| 意図確定後、dispatch前に停止 | ACTIVEと枠が残る。同じID再送は再実行せず、期限後に隔離・照合 |
| 起動成功、応答消失 | QUARANTINEDと枠を維持。新GET_STATEで同じIDの状態を確認 |
| 外部成功後のDB失敗/プロセス停止 | ACTIVEまたは隔離。DB再開後、期限切れを確認し新世代で照合。データを巻き戻さない |
| 完了保存後の応答消失 | 同じID再送で履歴と現在状態を返す。副作用を繰り返さない |
| 停止失敗 | 停止済みと扱わず隔離。データと枠を保持し、強制停止しない |
| 古い応答/偽の期限延長 | StaleFenceで拒否。新しい状態を上書きしない |
| 所有者不一致/既知リソース消失 | 隔離のまま調査。自動作り直し・削除なし |

参照処理自体は同期呼出しであり、ブロック中のドライバーを強制中断する機能はない。
別管理者による期限後照合と境界fencingが前提。現在は実通信がなく、実配備時には
有界RPCタイムアウトとjob追跡を実装する必要がある。通信エラーの生情報は結果に返さず固定コードにする。
レジストリ消失・復元・破損時は新規DBを作って再起動せず運用停止する。
古いバックアップから世代を巻き戻す復元、DBと境界記録の不一致も管理者照合が必要。

## テスト実行と結果

既存データを使わない別worktreeで、PowerShellから以下を実行する。
Python環境は既存の開発用venvのpython.exeを指定できる。DBとコンテナ実験の環境変数を空にする。
実機の診断コマンド、Composeのup/down、VM作成は実行しない。

```powershell
Set-Location <新しいworktree>\backend
$env:PROJECT_TEST_DATABASE_URL = ''
$env:EDITOR_LAB_IMAGE_ID = ''
python -m unittest discover -s tests -p test_vm_manager.py -v
python -m unittest discover -s tests -v
python tools/editor_foundation_check.py
```

2026年11月18日のWindows / Python 3.14での結果：

- 全体286件：**成功124件、スキップ162件、失敗0件**。
- 追加した管理ロジック38件はすべて成功。別プロセス8並列の枠制限、同一操作IDの4並列、
  同一所有者・別操作IDの4並列、意図確定後/外部副作用後の子プロセス停止を含む。
- 既存テスト248件のうち成功86件。現在所属を削除した直後の認可拒否、
  HTTP/WebSocketの実行不可、Firebase検証例外の秘匿、設計の保護ハッシュ検査も回帰確認。
- 第8段階checker：design_consistent、runtime_source_unchanged=true。
  editor_launch_enabled/deployment_allowed/real_infrastructure_verifiedはすべてfalse。
  実機証跡23項目はNOT_RUNのまま。
- Linux専用の安全なファイル操作、一時PostgreSQLのマイグレーション/並行API、
  opt-in実コンテナ試験は条件未充足または今回禁止の実接続のためスキップした。
  PostgreSQL上の新しいDDLは追加も実行もしていない。
- FastAPI/Starletteの既存TestClient依存に非推奨警告あり。今回依存パッケージは変更していない。

既存Compose検証は合成値・空envで`config`を解析するだけで、
Engineへ接続しない。Linux専用Gitストレージ・一時PostgreSQL・実コンテナの試験は今回実行しない。
一時SQLiteの成功は本番KVM・TLS・保存領域隔離の合格証拠ではない。

## 今後の実装前に止めて確認する事項

本番管理DB（専用PostgreSQL等）とHA・枠の権威、8人分以上の容量/バックアップ予算、
境界側fencingの実現方法、既存workspaceの安全な移送/提出ブリッジは未決定。
既存データや構成に影響する選定・移行は今回行わず、候補と推奨案を提示して承認を得る。
専用Linuxホスト、ゲスト外遮断、独立HTTPS・Firebase連携・失効、実障害復旧の検証が完了するまで
既存ゲートと実ドライバーの拒否を維持する。現時点では導入・起動・公開の操作手順は実行不可。
将来の導入は[第8段階手順](editor-foundation/runbook.md)と
[合格/停止条件](editor-foundation/verification.md)を満たす別PRで行う。
