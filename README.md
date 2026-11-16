# 初心者向け統合型チーム開発・進捗管理システム

チーム別プロジェクトAPIの仕様・DB反映手順・テスト方法は
[プロジェクト管理APIの説明](docs/project-management.md)を参照してください。

プロジェクト別の共有Gitと個人作業領域の初期化・状態確認については
[共有Git・作業領域の設計とAPI](docs/project-git-workspaces.md)を参照してください。

個人別code-serverについては、現在の環境で確認できた制約と今後の構成を
[第2段階の設計・事前検証](docs/user-editor-design.md)にまとめています。
個人エディタの起動・接続機能はまだ有効になっていません。

コード変更の提出・レビュー依頼の一覧・詳細・差分APIと、途中失敗時の復旧方法は
[第3段階のレビュー依頼API](docs/change-requests.md)を参照してください。

提出済みの変更に対する承認・差し戻しAPI、判定の同時操作対策、DB移行と検証結果は
[第5段階の承認・差し戻しAPI](docs/change-request-reviews.md)を参照してください。
