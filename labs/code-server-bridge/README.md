# 接続しないcode-server本文取得モック

PR #20のモックに、PR #21で手動検証専用のNode/workspace拡張を追加した。
Vueへのimportや既存code-serverへの自動導入はしない。送信処理・APIクライアントはない。
MockBridgeの既定は無効で、明示的なlabModeでもconnectionEnabledはfalse。
実行機能を有効にするフラグではない。

- capture.mjs：注入したdocumentの全文・言語・版・未保存状態を一回読む。
- bridge.mjs：注入したOrigin/source/Binding/generation/nonceの受理規則だけを検査する。
- bridge.test.mjs：実window/ファイル/拡張を使わない合成モック。
- auth-logging.test.mjs：既存Login.vueの信頼済みUIコードだけをモック依存で評価し、ログを検査。
  学生コードは評価しない。frontend/srcのconsoleと独自storageアクセスも静的点検する。

既存のNodeとfrontend/node_modules/@vue/compiler-sfcがある場合のみ、リポジトリ直下で実行する。
不足していても自動インストールしない。

```powershell
node --test labs/code-server-bridge/bridge.test.mjs labs/code-server-bridge/auth-logging.test.mjs
```

PR #21の新規モックも含める場合は、上記へ`labs/code-server-bridge/probe.test.mjs`を追加する。

- package.json：明示コマンドだけを宣言する検証専用manifest。browser entryやWebviewはない。
- probe/extension.cjs：コマンドを登録する入口。登録時には本文を読まない。
- probe/controller.mjs：専用Linux環境・モーダル確認・文書の前後照合を行う。
- probe/file-scope.mjs：本文を読まず、UID・実体パス・通常ファイル・リンク数・FDを確認する。

検証ゲートは既定無効。承認済み専用Linuxホストの非rootユーザー、専用root、
TEAM_PROGRESS_EDITOR_PROBE=ISOLATED_LAB_ONLY等を担当者が明示した場合だけ動作する。
これらの値は本番認可の代わりではない。詳細は[実エディター検証手順](../../docs/code-server-real-editor-probe.md)。

実code-serverの未保存コード・symlink/所有者・Webview・ブラウザー通信はまだNOT_RUN。
PR #20の範囲は[以前の設計資料](../../docs/code-server-bridge-feasibility.md)を参照する。
