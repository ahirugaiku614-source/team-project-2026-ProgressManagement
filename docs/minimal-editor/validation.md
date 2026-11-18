# 1人→2人→4人の実機検証計画（全件NOT_RUN）

今回は実機を作成・起動しない。次の手順は学校/管理者の承認と専用試験基盤が準備できた後だけ実施する。
既存DB/Docker/Git/workspace/volume、実Firebase資格情報、実利用者データを試験へ流用しない。
初期の全体上限4と実行ゲートを変更せず、別試験系で段階ごとに判定する。

## 準備するもの

- KVM/runsc/microVMごとの固定revision/image digest、更新済みkernel、guest非root、外側ACL/metadata拒否、
  本人diskだけのmount、管理socket/鍵なしを監査する。runscは非root全経路とfallback拒否を確認する。
- 新規の独立Linux試験機/volume、限定manager・read-only ready証跡、独立HTTPS/実watchdog、
  現在所属の試験認可サービスを用意する。実Firebase試験は専用projectで別承認後に行う。
- 新規合成User/Project、root16/data20GiBのquota、host/管理予約、backup先/鍵/manifest/復元先を記録する。
  cloud最小diskが大きければ物理予約/課金も増額し、合成試験でも無料枠超過を無断で許可しない。
- Windows側では専用worktreeからmock/静的検査のみ実行可能。実プログラムは隔離確認後のguest内だけで動かす。
  既存code-serverへ接続したり、ホスト上で未検証のユーザーコードを実行したりしない。

実制限の候補はKVMのvCPU/固定RAMとhost側cgroup帯域・IO上限、runscの委譲済みcgroup v2
（cpu.max/memory.max/pids.max等）、microVMのvCPU/RAM設定とjailer/cgroupである。
diskはゲストから変更できないhost側block/FS quotaで制限し、root16/data20GiBのimage上限と
FS metadataを含む実際の保存可能byte数を測る。単にdirectoryへchmodするだけでは隔離・容量制限としない。
これらの設定はまだ生成/適用しない。予約モデルの整数検査が成功しても実制限済みとはしない。

## 固定する合成workload

将来の試験は依存download・Internet接続なしで実行する。以下の短いソースを新規試験領域へ作る。
これは今すぐ実行する指示ではない。compiler/interpreter/editorの版、source hash、所要時間、peak RSS、
OOM/exit status、CPU/IO/容量/WS応答を記録する。

| workload | 具体的内容 | 確認すること |
| --- | --- | --- |
| エディタのみ | 合成100行/10fileを開き、保存/検索/補完、terminal起動。拡張なし→承認済み最小language server | cold/warm起動、操作遅延、RAM、browser側とserver側の分離 |
| Python | `print(sum(range(100000)))` と標準libの合成JSON処理。外部libraryなし | 単独とeditor併用、異常終了/停止後の保存 |
| C/GCC | `#include <stdio.h>` と `int main(void){puts("lab");return 0;}` を単一file -O0で逐次compile | compile/実行、disk/peak RSS。並列buildは次の別負荷 |
| Node | `console.log(Array.from({length:1000},(_,i)=>i).reduce((a,b)=>a+b,0))` | 単純JS。npm install/buildやnative addonの互換性は別試験 |
| Git | 合成小repoのinit/status/diff/local commit。hook/外部remoteなし | 本人領域内だけ。既存bare、提出snapshot、他人領域に影響しない |
| 同時負荷 | editor＋各言語を1種類ずつ、その後許可した4人の混在負荷 | OOMや長時間応答停止を隠さない。低RAMでswapに依存しない |

言語処理の合格はHello Worldだけで全ツール互換性を保証するものではない。
1 CPUで動けば公式推奨より低い構成の限定結果として記録し、全員への配備既定値へ自動昇格しない。

## 段階と合格/停止条件案

| 段階 | 実施内容 | 合格条件案/次段階へ進めない条件 |
| --- | --- | --- |
| 0：mock | 全profileの1/2/4予約、所有者/世代/idle/不明/並行競合、保護ハッシュ | 全回帰成功。実機はNOT_RUNのまま |
| 1：1人 | 2 CPU/4GiB基準→2 CPU/2GiB→1 CPU/2GiB→1 CPU/1GiB。各15分×3回 | warm editor起動≤120秒、保存/terminal操作p95≤1秒を暫定案。短い言語workload≤10秒、OOM0、本人外アクセス0、backup復元hash一致 |
| 2：2人 | 段階1合格profileのみ。別User/別Projectを同時に使い、保存/停止/再開を各10回 | host予約/quota内、他人のmount/process/network参照0、データhash一致。CPU/RAM/保存のどれか不足なら拒否して終了 |
| 3：4人 | 段階2＋host予算が足りる場合のみ。4組混在負荷と5組目拒否 | 上限≤4、5組目のVM/disk副作用0、不明状態も予約保持。Oracle2 OCPUで基準不成立なら実施しない |

全段階で既存の[第11段階安全性試験](../school-pilot/validation.md)と
[第8段階合否表](../editor-foundation/verification.md)を満たす必要がある。
新profileの採用で安全基準を減らさない。数値基準は案で、実測していない。

## アイドル/保存/失効の必須試験

- client0でもterminalのsleep/長時間compile/background jobがあれば停止禁止。
  infrastructureの常駐processとユーザーjobの識別を検証し、識別不能なら拒否する。
- dirty buffer、autosave中、保存ack消失、ブラウザ切断、複数tabのうち1つ未保存では停止禁止。
  CPU低下やWS無通信をdirty/jobの代用にしない。
- 閉じた画面の最後の保存結果が不明な場合、外部から「0」と申告して解放しない。
- 停止提案後に新job/再接続/編集が発生した場合、同じrevision/nonceで停止できないことを確認。
  実境界は新規受付を原子的に凍結してからflush/静止を確認し、期限切れなら停止をやり直さない。
- stop成功応答消失/timeout/DB failureで枠を保持する。disk再作成/強制停止/データ削除で復旧しない。
- generation変更後に旧HTTPS sessionと旧操作が拒否され、最大15秒内に実WS/streamを切断する。
  モックの即時拒否を実ソケットの切断保証に読み替えない。
- root/data quota、inode/thin metadata、CPU/memory/process/IO制限とOOMを実測。
  容量閾値超過や監視不能なら新規起動拒否し、guest background書込みへの外側制御も確認する。

停止条件：他人/共有Git/DB/秘密への到達、管理socket公開、隔離fallback、容量不足、保存不明、
quota/fence超過、backup不成立、認可照会不能時の許可、失効切断不達、実行不明の自動解放。
試験対象だけの受付を止め記録保全する。既存稼働環境にstop/down/prune等を実行しない。

## 未実装・未検証のまま残す項目

実作成/停止とidle集約器・freeze/flushのatomicity、実fencing、実quota・監視、未知状態からの復旧、
ARM64 toolchain/拡張、code-server browser互換性、実cloud契約/quota/回収、実disk永続化と外部backup復元、
Firebase/HTTPS/mTLSと実15秒切断、既存workspace移送/提出ブリッジ。
本PRの目的はそれらを無理に実装することではなく、合成データで検証できる契約と準備を作ること。
