# Stage 10-D 実装・検証記録

## 実装

実Firebaseなしで製品の変更管理APIを動かす専用 `synthetic_asgi` を追加した。通常mainはFirebaseを要求し続ける。旧個人エディタ認証・実行APIは通常mainに登録し、元の認証ソースを変更していない。共通APIの処理は旧HEADとのAST比較で、認証importとrouter登録位置を除いて一致した。

A/Bの独立passwordをPBKDF2で検証し、実SQLiteのopaqueログイン台帳を使用する。編集セッションをログインへ結合し、失効を収集認可・cookie gate・継続WebSocket検査へ伝播する。合成認証は別appへの明示注入であり、通常画面・通常起動のfallbackではない。

実APIへ接続する合成実機専用Vue入口、二段階の設定生成器、既存layoutの読取り監査、新規設定だけの設置、digest必須backend Dockerfile、build終了確認器、専用Compose差分を追加した。方式Cとmode 666拒否、通常providerの既定拒否は維持する。

## Windows検証

- バックエンド：52モジュール、最終集計 **824 PASS / 0 FAIL / 0 ERROR / 170 SKIP（994件）**。
- 集計方法：全モジュール実行と、修正後の影響suite再実行を組み合わせた集計。一括実行が最初から全成功したという意味ではない。
- 途中の一括結果は991件、791 PASS / 16 FAIL / 15 ERROR / 169 SKIP。旧エディタ認証ソースの不変条件とFirebaseの残存import経路による31件を検出した。旧ソースを元の内容・改行形式へ戻し、旧router登録を通常入口に限定した後、該当する診断・旧API 147件と合成認証APIを再検証して解消した。安全条件・期待結果・hash条件は変更していない。
- その後、合成認証・API・編集セッション30件（追加の権限試験2件を含む）がPASS。実SQLite、製品の認可と編集セッションを使用し、Firebase資格情報や実環境を使用していない。Windows試験の所有者検査だけは明示的なfixtureに分離しており、Linuxのmode保証ではない。
- SKIP：従来Linux固有145件、PostgreSQL固有23件、Docker opt-in 1件に加え、実Linux Gitロック＋PostgreSQL行ロックの新規競合試験1件。最後の1件は両環境が必要で、未実行。
- 最初の全体ランナーは240秒上限でsaved snapshot suiteが時間切れとなり、製品の期限を変えずランナーだけ900秒へ延長した。再実行では当該18件を含む全モジュールが完走した。Windows multiprocessing試験はstdinでなく `python -c` 入口を使った。
- フロントエンド：**77 PASS / 0 FAIL / 0 SKIP**。従来71件＋合成実機認証6件。
- 新規配置・成果物ツール：**20 PASS**。偽のcommit IDを記載しても、信頼済みGitと異なるソースを受理しない試験を含む。
- code-server拡張：**10 PASS**。誤って対象0件となった呼び出しは検証結果に含めず、実際の `extension.test.cjs` を実行した。
- 通常build・memoryデモbuild・合成実機buildは成功。通常build約638.64 KBのチャンク警告は既知の改善事項。lint・型検査は未設定。

新規実機認証の主な確認対象は、A/B独立認証、UID偽装拒否、Origin照合、失効と期限、試行制限、平文token非保存、旧DB非上書き、所属解除・他Task拒否、ログインと編集セッションの結合・失効である。方式Cの固定後変更・明示確認・再収集禁止・既存レビューの試験も既存suiteで再実行した。

## 保全と残課題

旧worktree7ファイルは既存バックアップと直接バイト比較して全件一致、バックアップ10ファイルと保護対象一時領域9件を確認した。Linux、PR #21・#23、実DB、実Firebaseには接続していない。commit・push・PR更新もしていない。

Linuxの既存174 PASS / 1 SKIPは利用者から共有された前工程の結果。今回の新ASGI・fixture・設定設置・build・nginx・ブラウザー操作の成功とは区別する。実TLS/WebSocket失効、保存後mode、rootless設定設置、PostgreSQL/Git実競合、新imageのbuild完了・固定digestの実証が残る。

配置手順は `docs/stage10d-handoff.md`。通常の実providerは既定拒否で、合成専用入口だけが承認済み固定設定から接続する。合成用DB markerは通常22表に専用1表を追加するが、既存マイグレーションは変更していない。既存DB・volumeを修復・上書きする操作は追加していない。

A2未完了、B1未承認、SSH255原因分類Hを維持する。
