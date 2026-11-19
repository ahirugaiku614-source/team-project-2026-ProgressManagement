# 接続しないcode-server本文取得モック

本ディレクトリはPR #20の検証用。Vueやcode-serverへ登録/importしない。
拡張manifest・起動処理・送信処理・APIクライアントはない。MockBridgeの既定は無効で、
明示的なlabModeでもconnectionEnabledはfalse。実行機能を有効にするフラグではない。

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

実code-serverの未保存コード・symlink/所有者・Webview・ブラウザー通信を実証するものではない。
詳細とNOT_RUN項目は[設計資料](../../docs/code-server-bridge-feasibility.md)を参照する。
