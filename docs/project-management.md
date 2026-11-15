# チーム別プロジェクト管理API

## 現在の構成

今回の作業開始時点では、GitHubの`main`とローカルは同じコミット
`c4ef8d8`（code-serverのDockerfile追加）でした。

- `frontend/`: Vue / Vite。Firebaseログインと画面遷移の認証チェックがあります。
  `Editor.vue`は個人所有のコードファイルAPIと接続済みです。
  `Home.vue`のプロジェクト表示は仮の内容で、`CodeSharing.vue`は表示確認用です。
- `backend/app/main.py`: Firebaseユーザー同期、個人タスク、チーム作成・参加・一覧、
  メンバー一覧、チーム共有タスク、個人所有ファイルの作成・取得・更新・削除があります。
- `backend/app/models.py`: SQLAlchemyのモデル定義。
  今回、既存のUser / Team / TeamMember / Task / CodeFileにProjectを追加しました。
- `backend/app/schemas.py`: APIの入力・出力形式を定義します。
- `docker-compose.yml`: FastAPI、Vue、PostgreSQL、code-serverを起動する構成です。
  code-serverは専用Dockerfileと共通の作業ボリュームを使っています。

今回の変更はプロジェクトの基本情報を管理するバックエンドAPIです。
プロジェクト別フォルダ、code-server連携、Gitによる共有、Vue画面との接続は今後の作業です。
アプリ利用者のGitHubアカウントやGitHub操作は必要ありません。

## 追加したデータ

`projects`テーブルに以下を保存します。

| 項目 | 意味 |
| --- | --- |
| id | 自動で割り当てるプロジェクト番号 |
| name | プロジェクト名。前後の空白除去後に1〜100文字。DBモデルはString(100) |
| description | 説明。省略またはnullが可能 |
| team_id | 所属チームの番号。teams.idへの外部キー |
| created_by | 作成者のアプリ内ユーザー番号。users.idへの外部キー |
| created_at | 作成日時。既存モデルと同じUTC形式 |

`Team.projects`からチームのプロジェクトを、`Project.team`から所属チームを取得できます。
`Project.creator`から作成者を取得できます。削除の連鎖設定は追加していません。
同じチーム内で同じ名前を使うことは可能です。

## APIの使い方

両APIとも`Authorization: Bearer <Firebase IDトークン>`が必要です。
ログイン後、まず既存の`GET /api/me`でアプリ内ユーザーを登録してください。
対象チームのオーナーと一般メンバーの両方が利用できます。

### 作成: POST /api/teams/{team_id}/projects

例えば、チーム番号1にプロジェクトを作成する場合は次のJSONを送信します。

```json
{
  "name": "チーム開発アプリ",
  "description": "初心者向けの共同開発プロジェクト"
}
```

成功時はHTTP 201で、次の形式のJSONを返します。

```json
{
  "id": 1,
  "name": "チーム開発アプリ",
  "description": "初心者向けの共同開発プロジェクト",
  "team_id": 1,
  "created_by": 2,
  "created_at": "2026-11-15T00:00:00"
}
```

`team_id`はURLから、`created_by`は認証済みユーザーから決めます。
JSONにこれらを送っても、別チームや別ユーザーとして登録することはできません。
保存に失敗した場合はDB処理をロールバックします。

### 一覧: GET /api/teams/{team_id}/projects

対象チームのプロジェクトだけを新しい順に返します。同じ作成日時ならidの降順です。
返却値は上記JSONの配列で、未作成の場合はHTTP 200と`[]`を返します。

### エラー

| HTTPステータス | 状況 |
| --- | --- |
| 401 / 403 | 未認証、不正な認証ヘッダー。既存のHTTPBearer設定に従います |
| 401 | 無効または期限切れのFirebaseトークン |
| 404 | アプリ内ユーザーまたは対象チームが存在しない |
| 403 | ログインユーザーが対象チームに所属していない |
| 422 | 名前がない、空白のみ、型が異なるなど、入力形式が不正 |

## DBへの安全な反映

既存コードはFastAPIの起動時に`Base.metadata.create_all(bind=engine)`を実行します。
今回はこの仕組みを維持し、存在しなければ`projects`テーブルと索引を追加します。
新規DBでは`projects.name`を`VARCHAR(100)`として作成します。既存テーブルは自動変更しません。

1. 通常の運用手順に従ってDBのバックアップを確保します。既存DBがある場合は下記の移行手順も確認します。
2. 下記の環境変数を設定し、ルートで`docker compose up -d --build backend`を実行します。
3. `http://localhost:8000/docs`で追加された2つのAPIを確認します。
4. FirebaseのIDトークンで認証し、所属チームで作成と一覧を試します。

Firebase秘密鍵は既存の設定を使用します。DB接続設定は次の環境変数から取得します。
`.env`と`backend/firebase-credentials.json`はGitの除外設定に含まれており、今回の変更にも含めません。

`create_all`は既存テーブルの構造を変更する機能ではありません。
既に別の構造の`projects`テーブルが存在する場合や、今後カラムを変更する場合は、
別途マイグレーションが必要です。

### 既存のprojectsテーブルの文字数制限

`backend/migrations/001_project_name_length.sql`は、既存PostgreSQLの`name`を
`VARCHAR(100)`へ変更する手動適用用SQLです。今回、稼働中DBには適用していません。

まず`SELECT id, char_length(name) FROM projects WHERE char_length(name) > 100;`で
制限を超えるデータの有無を確認してください。該当があれば、利用者と名前の変更方針を決めるまで
適用を止めます。SQLはテーブルをロックして再確認し、101文字以上があれば例外で中止します。
文字列の切り詰め、データ削除、名前の自動変更は行いません。

バックアップと適用内容の確認後、対象DBへ`psql -v ON_ERROR_STOP=1 -f`でこのSQLを適用します。
トランザクション内で変更するため、失敗した場合はロールバックします。
適用中はテーブルへのアクセスを待機させるため、運用への影響が少ない時間帯に実施してください。
DBボリュームを削除したり、DBを作り直したりする必要はありません。

### DB接続の環境変数

| 環境変数 | 用途 |
| --- | --- |
| DATABASE_URL | 直接起動時の接続URL。指定すれば他のDB設定より優先 |
| POSTGRES_USER | DBユーザー。DATABASE_URL未指定時は必須 |
| POSTGRES_PASSWORD | DBパスワード。DATABASE_URL未指定時は必須 |
| POSTGRES_DB | DB名。DATABASE_URL未指定時は必須 |
| POSTGRES_HOST | ホスト名。省略時はdb |
| POSTGRES_PORT | ポート。省略時は5432 |

Composeはルートの`.env`から`POSTGRES_USER`・`POSTGRES_PASSWORD`・`POSTGRES_DB`を読み取り、
backendとdbの両サービスに同じ値を渡します。backendの接続先はサービス名`db`、内部ポート5432です。
Composeで起動するbackendには`DATABASE_URL`を渡さず、この3項目を共通の設定元にしています。
必須値がなければ起動前にエラーになります。実際の値を含む`.env`や接続URLはコミットしません。

ローカルでPythonを直接起動する場合、`.env`は自動読込されないので、シェルに環境変数を設定してください。
ホスト名には`127.0.0.1`、ポートには公開ポートを指定します。
個別の変数からのURL組み立てには`URL.create`を使い、パスワードに`@`や`/`などが含まれても扱えます。
`DATABASE_URL`を直接指定する場合は、URL中のユーザー名・パスワードを適切にエンコードしてください。

既存PostgreSQLボリュームのユーザー・パスワード・DB名を環境変数の変更だけで更新することはできません。
既存DBで実際に使用している値を設定してください。今回、資格情報や既存ボリュームは変更していません。

## プロジェクト削除と関連データの方針

今回提供するAPIは作成と一覧だけで、削除APIは追加しません。
外部キーやORMの削除カスケードも変更しません。

- `Project.team_id`と`Project.created_by`はNULL不可で、DBの`ON DELETE CASCADE`はありません。
  プロジェクトが残ったまま親チームや作成者をDBで削除すると外部キー制約で拒否されます。
  ORM経由のチーム削除も、関連プロジェクトの外部キーをNULLにできず失敗します。
- ProjectをDB／ORMで削除しても、参照先のTeamやUserは削除されません。
  現在のTaskとCodeFileには`project_id`がなく、プロジェクトの子データではありません。
  プロジェクト削除によってタスク・ファイル・メンバーを削除する仕組みはありません。
- 開発フォルダ、Gitリポジトリ、code-serverの作業領域もまだProjectと関連付けていません。
  DB上の削除に連動したディスク上のファイル削除は行いません。

今後、削除機能を実装する際は以下を設計します。

1. 作成者・チームオーナーなどの削除権限と、確認画面・監査履歴。
2. 論理削除やアーカイブ、復元・保持期間の採用。
3. `project_id`を持つ子テーブル追加時のRESTRICT／CASCADE／SET NULLの選択と移行。
4. 作成者が退会した場合やチームを削除する場合の所有権移管・記録保持。
5. 編集・共有中の削除防止と、DB処理とフォルダ／Git／code-serverの後処理の整合性。
   外部の削除はDBトランザクションだけで元に戻せないため、再試行可能な処理が必要です。

実データの削除や既存の関連付け変更が必要になる場合は、実行前に確認します。

## テスト

`backend/`で実行します。テストはFirebaseの外部トークン検証だけを代替し、
認証ヘッダーの確認、チーム所属判定、HTTPリクエスト、DB保存処理は実際のコードを通します。
実Firebaseの認証情報は不要です。

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

通常はメモリ上のSQLiteを使い、実DBへ接続しません。
PostgreSQLでも確認する場合は、テスト用DBのURLを指定します。
ランダムな専用スキーマ内で動作し、検証終了後にそのスキーマを削除します。
指定するDBユーザーにはスキーマ作成権限が必要です。

```powershell
$env:PROJECT_TEST_DATABASE_URL = 'postgresql+psycopg2://USER:PASSWORD@localhost:PORT/TEST_DATABASE'
python -m unittest discover -s tests -v
Remove-Item Env:PROJECT_TEST_DATABASE_URL
```

既存16件のテストで、作成・一覧、チーム間の分離、所属確認、認証失敗、入力チェック、
作成者の偽装防止、ロールバック、外部キー、既存APIの基本動作を確認します。
既存データ入りの旧構造にテーブル作成処理を2回適用し、データが保持されることも検証します。
実Firebaseとの通信とブラウザ操作はこの自動テストの対象外です。

追加テストで空白除去後の100文字／101文字（日本語を含む）、OpenAPIとDBモデルの上限、
DB設定の必須値・優先順位・特殊文字、削除時の外部キーと親データ保持を確認します。
Docker CLIがある環境では、実`.env`を読み込まず仮の値でCompose設定の整合性も確認します。
PostgreSQL専用の2件は既存テーブルへの移行を検証し、SQLite実行時はスキップします。

既存コードと同じPydanticのConfig形式・UTC日時処理、およびTestClientのhttpx利用により、
依存ライブラリから非推奨警告が出る場合があります。

### 今回の実行結果（2026年11月15日）

- 初回実装: SQLite・PostgreSQL 15の各16件すべて成功。
- レビュー対応後: 既存16件＋追加12件、計28件。
  PostgreSQL 15（一時コンテナの専用スキーマ）で28件すべて成功。
  SQLite（メモリ上）では26件成功、PostgreSQL専用の移行テスト2件をスキップ。
- Composeの設定確認は仮の環境変数と空のenvファイルを使用し、実`.env`は読み込んでいません。
- 既存の無制限文字列からVARCHAR(100)への移行、再適用時のデータ保持、
  101文字の既存名がある場合の中止と原文保持、DB自体の100文字制約を確認しました。
- 既存のDB・Firebase認証情報はテストに使用していません。
- 稼働中DBへの移行適用、既存データの削除、削除APIの追加は行っていません。
- `git diff --check`: 成功。
