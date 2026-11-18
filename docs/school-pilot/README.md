# 第11段階：学校内試験の構成・費用・検証計画

登録30〜40人のWeb/API利用と、全体4台のVMエディタ利用を分けた設計・比較である。
最新main `b634d65`（PR #12マージ済み）から新規ブランチで作成した。
既存DB/Docker/Git/workspace/volume/認証設定とVM管理・gatewayは変更していない。
実行無効ゲートは維持し、実VM・公開・契約・課金は行わない。

| 文書/ファイル | 内容 |
| --- | --- |
| [構成・保存・学校登録・拡張設計](architecture.md) | A/B/C、32/64GiB、40/80環境、thin/base共有、全体4枠、10/20台、招待/mail比較 |
| [費用比較](costs.md) | 公式価格の参考点と仮定予算を区別。電力・媒体・運用工数・初期資金を計上 |
| [安全性・性能試験計画](validation.md) | 全項目NOT_RUN。測定方法・合格/停止条件・隔離環境・RPO/RTO |
| [計算用JSON](plan.example.json) | DESIGN_ONLY。実接続先・資格情報・起動設定は持たない |
| `backend/tools/school_pilot_check.py` | 安全なJSON読取りと保護ハッシュ検査を再利用する静的試算 |
| `backend/tests/test_school_pilot.py` | 一時JSON・モックによる計算/安全条件の検証 |

推奨案は、Web/APIを別にした32GiB専用Linuxホストからの低コスト試験。
初期全体4枠、40 User×Project保存なら主保存**実効2TiB**、外部backup各組**実効4TiB**を計画する。
1TiBではdata800GiB以外の領域と空き予算が足りない。1人が2Projectを使う80環境なら再計画が必要。
32GiBで性能試験を満たせなければ64GiB案を比較し、価格理由で隔離・backupを削らない。

学校限定参加は名簿確認付き招待を初期推奨とし、全員に学校mailがある場合は確認を併用する候補。
学校固有のdomainを全APIへ固定せず、将来の組織単位ポリシーとして設計する。
どちらの認証方式も変更・有効化していない。現在のアプリを学校限定運用可能とは判定しない。

## 静的検証結果

2026年11月18日、Windows/Python 3.14で検証した。

- 新規静的16件：成功16件、失敗0件。一時JSONとモックのみを使用。
- 全回帰347件：**成功185件、スキップ162件、失敗0件**。
  既存331件の成功169件と新規16件が成功した。Linux専用ストレージ、一時PostgreSQL、
  opt-in実コンテナ試験は環境/安全条件によりスキップし、既存DB/Engineは利用していない。
- 新checker：design_consistent、40環境の主保存1584GiB/backup3040GiB、1TiB不足を確認。
- 既存checker：design_consistent、runtime_source_unchanged=true。
  起動/配備/実基盤確認はfalse、実機23項目はNOT_RUNのまま。
- 既存TestClient依存の非推奨警告あり。依存更新は対象外。

容量計算の成立は実機性能/安全性の合格ではない。
実行・配備・認証変更・性能検証の許可値はすべてfalse。実基盤の性能・安全性試験は未実施のまま。
