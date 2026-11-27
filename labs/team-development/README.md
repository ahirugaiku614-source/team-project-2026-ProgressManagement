# Stage 10向け中央サーバー構成

今回Docker・Linuxで実行していない構成案です。PR #21検証環境とは完全に分離します。
各利用者のPCにDocker等を配布せず、中央サーバーのTLS入口をブラウザーで利用します。

## 管理側の準備順序

1. 新規隔離ホスト／パス、image digest、A/Bで異なる非root UID/GIDを管理者が確定する。
2. PostgreSQLの005～009を隔離DBで順番に検証する。今回実DBには適用していない。
3. メンバー別管理ワーカーの専用rootを、その固定メンバーUID/GID、0700で新規作成する。
   ワーカーとcode-serverは別コンテナ・別PID namespaceで、学生側へ専用rootは渡さない。
4. `LinuxWorkspaces(root, member_uid, member_gid, member_id=DBのID, project_id=DBのID)`を
   固定対応のfactoryからだけ選択する。DB IDとOS UIDを同一と推測しない。
5. TaskReviewServiceが検証済みtreeを渡して作った`w-UUID`だけをメンバーへbind mountする。
   隣接する`m-UUID.json`、管理root、Git、DB、Docker socketは渡さない。
   ファイルは0600（実行属性あり0700）、ディレクトリは0700で所有者を統一する。
   グループ書込みを許して既存収集検査を緩めない。
   FastAPI→管理ワーカーの初期化RPC/配置bootstrapは未接続で、別途実装・実証が必要。
6. 新しい管理済みcode-server imageに`labs/code-server-submit`を読み取り専用で同梱する。
   `TEAM_APP_ORIGIN`とTeam/Project/Taskは管理側が設定する。認証情報は環境変数へ置かない。
7. A/Bごとの認証設定・editor-data・固定workspaceを新規に用意する。既存領域は上書きしない。
8. gatewayは同一originの`/CodeSharing`と`/api/`を既存Vue/FastAPIへ接続する。
   `/code-server/<固定workspace UUID>/`を対応する一つのupstreamにだけ接続し、
   nginx `auth_request`から`GET /api/editor-gateway/<同UUID>`へCookieを転送する。
   204以外では接続を拒否する。クライアント指定host・URL・pathをproxy_passへ渡さない。
   TLS、WebSocket upgrade、path prefix、Cookie非記録、Origin制限を検証する。
9. `WorkEnvironment(editor_gateway=True)`は、認証済みgatewayと管理側authorityの
   実証後にサーバー側bootstrapからのみ注入する。現在のアプリは既定拒否のまま。
10. 承認後にだけ`docker compose -f labs/team-development/compose.yaml --profile stage10-approved up`
    を検討する。今回この操作は行っていない。停止対象は当該Compose projectだけに限定する。

## 起動を阻む未解決の前提

`EditingSessions`は永続セッション・scope・期限・失効・challengeを検証する実装ですが、
実code-serverの全ウィンドウとdirty状態を独立して証明するauthorityは未接続です。
利用者が変更できる拡張の申告だけで接続してはいけません。管理側の一意な編集経路、
実プロセス／セッションの認証、複数接続の把握、失効時のWebSocket切断を実証してください。
短命Cookieだけでは既存WebSocketの強制切断を保証しません。未解決なら実利用者へ公開せず、
合成環境だけに限定します。収集callbackの強制期限も未解決であり、有効化条件です。

## Linux検証項目

- A/B UID/GID、グループ書込み、管理root非公開、他workspaceへの移動不可。
- symlink/hardlink/特殊ファイル、同一FSのbind mount、FDのmount ID、変更中の二回照合。
- 空間・件数・深さ・サイズ上限、初期化中断時の残存と同requestでの再実行拒否。
- 未保存／未提出変更の更新拒否、旧世代の保持、mainの更新と基準の固定。
- cookie期限・失効・別workspace転用、TLS・WebSocket、Firebase失効との連携。
- PostgreSQLの並行受付、DB/Gitロックの期限、GitとDBの応答喪失、再起動後の証拠照合。

既存の`labs/saved-submission/compose.yaml`の管理側UDS・台帳構成を再利用します。
この新規Composeは編集コンテナとgatewayの構成案であり、自動で管理サービスを有効化しません。
