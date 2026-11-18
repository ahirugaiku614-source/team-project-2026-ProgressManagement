# 第12段階：最小エディタの設計・資源予約検証

PR #13マージ済み最新main `9ae147e`から作成した。実クラウド・VM・コンテナは使わない。
既存main作業ツリーを保護した別worktreeで、新規コードを`backend/minimal_editor_lab/`へ隔離した。
既存API/VM manager/gatewayへのimport・登録・接続は追加せず、DB/DDL・Compose・保存領域も変更しない。

| 区分 | 今回の状態 |
| --- | --- |
| 実装済み | 4profile、CPU/RAM/保持diskの原子的予約、全体最大4枠、重複/所有者照合、停止証跡とFence照合、独立一時台帳、予約シミュレーションCLI |
| モックで検証 | 起動/停止/世代/保存、ジョブ/dirtyによる停止拒否、期限切れ/不明時の隔離、複数プロセス競合、gateway型/世代契約 |
| 未実装 | 実作成/停止、cgroup/FS quota、活動観測/flush/凍結、保存のコピー/移送、隔離解除/復旧、実Firebase/クラウド/HTTPS接続 |
| NOT_RUN | 全profileのcode-server/Python/GCC/Node/Git性能、実KVM/gVisor/microVM、実永続disk、実切断/バックアップ復元 |

[比較・無料枠の評価](evaluation.md)と[1→2→4実機検証計画](validation.md)を参照する。
次の実機候補は2 CPU/2GiBと基準2 CPU/4GiBの比較。1 CPU案は公式推奨CPU未満の低優先候補であり、
無料枠に収まりそうという理由だけで採用しない。全体4は上限で、資源不足時に4まで起動できる保証ではない。

## コードの入口

| ファイル/クラス | 何をするか |
| --- | --- |
| `contracts.py`: Profile/Budget | 比較対象の予約量、制御系・安全余裕を差し引いた予約可能量 |
| `contracts.py`: StorageEvidence/IdleEvidence/StopAck | 保存可能量、全client保存・ジョブ静止、停止境界確認の契約 |
| `ledger.py`: Ledger | 新しい一時SQLiteだけに保存する台帳。複数プロセスの予約を直列化 |
| `model.py`: ResourceModel | 初回disk予約、同じdiskの再開、停止意図、確認後だけCPU/RAM枠解放 |
| `report.py`: simulate/evaluate | 1/2/4組の予約を模擬し、実機結果を常にNOT_RUNで出力 |
| `tests/minimal_editor_fakes.py` | 既存の永続VMモックと、最終停止時に活動revisionを再確認する境界モック |
| `tests/test_minimal_editor.py` | 一時データだけの資源/アイドル/世代/gateway/並行試験 |

各クラス・関数の先頭に日本語で役割を記載した。実ユーザーのファイルをモデルへ渡すAPIはない。
modelのRUNNINGは合成台帳値であり、`unavailable_route`は常にsafe_ready=false。
新規`require_real_runtime`と既存の実行無効ゲートは常に拒否する。

## 予約と停止の流れ

1. 本人actorとEditorBinding、現在所属の検証器を要求する。既定検証器は全拒否。
   実Firebaseを実装した別の認可経路ではなく、現在はモックだけが許可する。
2. `prepare_start`は全体枠、CPU、RAM、停止中を含む全disk予約を一つのBEGIN IMMEDIATE内で検査する。
   初回だけroot16+data20GiBを予約。停止後も保存予約は残り、再開で二重予約しない。
3. 保存証跡は5秒以内で健全な総commit上限を要求する。「実free byte」だけを繰り返し使うカウンターではない。
   既存予約の未使用分、FS/inode/thin metadata、空き余裕を考慮する実観測器はまだない。
   そのためモデルの予約成立を実diskの容量保証やquota適用済みとしない。
4. 同じoperation_idの再送は同じ本人/操作/profileなら履歴だけを返す。新しい実行Fenceを返さない。
   別IDで同じUser×Projectを重複予約する要求は拒否する。停止済みからの再開だけ新世代で許可。
5. `prepare_idle_stop`は10分の無活動、client0、job0、dirty=false、saving=false、saved_ack=true、
   世代/本人/観測鮮度を要求する。Noneを0/falseへ変換しない。
6. 新規job・client・編集受付を凍結し、全bufferのflush/保存を確認した境界のnonce/revisionを停止意図に保存する契約。
   判断と実停止の間にjob/編集が始まったら境界側で拒否する。nonceは一回限りで、再開後に流用できない。
7. 既存Fence/WorkerCommand/Observationに加え、同じ保存/凍結のStopAckと停止確認が揃って初めてSTOPPEDを確定する。
   CPU/RAM枠を返してもdiskは保持する。停止応答消失/失敗/lease超過はQUARANTINEDで全予約を保持する。

凍結とflushはこのモデルが実装した実機機能ではない。実境界での原子的な受付停止、ジョブ追跡、
未保存buffer確認、lease/fencingの永続適用が必要で、ブラウザの自己申告やCPU低下だけでは証跡を作らない。
端末切断で保存結果が分からなければ停止しない。安全な節約ができず費用が残る場合も許容する。
基盤を有効にする前に「未確認なら自動停止しない」運用と代替保全手順を承認する。

## 既存契約との整合性と制限

- User×ProjectのVM IDは既存EditorBinding、disk IDは既存disk_keyを使用する。
  Firebase UID→UserとTeam/Project対応は将来の既存認可サービスからのみ確定する。
- 操作ごとにgenerationを増やし、30秒leaseをFenceへ保存。古い世代/偽のlease/別所有者の完了は拒否。
  このleaseはVMを止めたり枠を自動解放するタイマーではない。
- gatewayの最大5分/15秒の認可、Host/Origin・本人・世代照合を変更しない。
  契約試験でのみsafe_readyをmock注入し、再開後の旧セッション拒否を確認する。
- 純粋な予約モデルなのでCPU/RAM使用量をOSで強制する能力はない。cgroup/VM scheduler/FS quota/IO/プロセス数等は実機で別途必要。
  使用超過/OOMを停止成功と推測しない。先に保全・隔離する。
- 10分は停止判断の比較値で、自動停止するOSタイマー/定期ジョブはまだない。
  将来もprofileと上限は承認済みserver設定から選び、ブラウザの自由なリソース申告を実行設定へ採用しない。
- 停止不明から資源を解放する復旧は未実装。台帳を作り直して枠を戻さず、管理者照合と新世代fenceの別実装を待つ。
- 既存VMManagerの冪等startと、モデルの新ID重複予約拒否は異なる。後者は予約を増やさないための保守的方針で、
  既存APIの仕様を変更するものではない。両者を本番で二重台帳として同時運用しない。
- 私有ローカルSQLiteは試験参照方式。NFS/複数ホストは非対応。親ディレクトリ差替え/特権管理者への隔離は保証しない。
  markerと通常ファイル/単一link、O_EXCLで誤上書きを防ぎ、時計逆行/DB失敗は拒否する。
  壊れたDBや古いbackupからの自動復元・世代巻戻しは行わない。

## 再現手順と試験結果

別worktreeで、Pythonの開発venvを指定して実行する。以下は一時台帳と合成データだけを使う。

```powershell
Set-Location <別worktree>\backend
python -m minimal_editor_lab.report
python -m unittest discover -s tests -p test_minimal_editor.py -v
python tools/editor_foundation_check.py
$env:PROJECT_TEST_DATABASE_URL = ''
$env:EDITOR_LAB_IMAGE_ID = ''
python -m unittest discover -s tests -v
```

CLIの小型host予算は例として2CPU/12GiBから制御1CPU/2GiB、200十進GBの80%から
host root50GiB+scratch16GiBを差し引く。予約予算は1CPU/10GiB/83GiB。
1CPU profileは1人だけ予約でき、2CPU profileは0人となる。これは保守的な割当て例で、Oracleの必須overheadではない。
十分な予算8CPU/24GiB/1494GiBでは全profileで4組予約できるが、実行・性能・隔離の証拠にはしない。

実VM/実コンテナ/実code-serverは全件NOT_RUN。Python/GCC/Node/Gitの実測も各出力でNOT_RUNとする。
2026年11月18日、Windows/Python 3.14での最終結果：

- 全377件：**成功215件、スキップ162件、失敗0件**。
- 新規30件すべて成功。1/2/4の全profile予約、CPU/RAM/disk不足、重複/所有者、停止/再開、
  job/dirty/欠測、最終境界の競合、nonce再利用、応答消失、lease/DB failure、台帳再開、
  8プロセス競合、既存gatewayのready/世代契約、常時無効ゲートを含む。
- 既存347件：成功185件、スキップ162件。Linux専用ストレージ、一時PostgreSQL、
  opt-in実コンテナは環境/安全条件により未実施。既存DB/実Engineは利用しない。
- 既存foundation checker：design_consistent、保護3ファイルhash一致、実機23項目NOT_RUN。
  起動/配備/実基盤確認はfalse。新reportの全profile・各言語もNOT_RUN。
- バックアップ容量/参考費用をDecimalで再計算。git diff --checkも確認する。
- 既存TestClient依存の非推奨警告あり。依存更新は今回の対象外。

実機でのCPU/RAM/IOの十分性、保存可能容量、隔離/自動停止/復元の実証はまだない。
学校試験導入の許可や既存データ移行の許可としてこの結果を使わない。
