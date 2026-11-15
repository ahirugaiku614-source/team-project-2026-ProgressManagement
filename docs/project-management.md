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
| name | プロジェクト名。必須。前後の空白を取り除き、空白だけの名前は拒否 |
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
既存テーブルの変更やデータ削除、DBの初期化は必要ありません。

1. 通常の運用手順に従ってDBのバックアップを確保します。
2. リポジトリのルートで`docker compose up -d --build backend`を実行します。
3. `http://localhost:8000/docs`で追加された2つのAPIを確認します。
4. FirebaseのIDトークンで認証し、所属チームで作成と一覧を試します。

DB接続先とFirebase秘密鍵は既存の設定を使用します。
`backend/app/db/database.py`の既存の固定接続設定が、稼働中のDB設定と一致している必要があります。
`.env`と`backend/firebase-credentials.json`はGitの除外設定に含まれており、今回の変更にも含めません。

`create_all`は既存テーブルの構造を変更する機能ではありません。
既に別の構造の`projects`テーブルが存在する場合や、今後カラムを変更する場合は、
別途マイグレーションが必要です。

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

16件のテストで、作成・一覧、チーム間の分離、所属確認、認証失敗、入力チェック、
作成者の偽装防止、ロールバック、外部キー、既存APIの基本動作を確認します。
既存データ入りの旧構造にテーブル作成処理を2回適用し、データが保持されることも検証します。
実Firebaseとの通信とブラウザ操作はこの自動テストの対象外です。

既存コードと同じPydanticのConfig形式・UTC日時処理、およびTestClientのhttpx利用により、
依存ライブラリから非推奨警告が出る場合があります。

### 今回の実行結果（2026年11月15日）

- SQLite（メモリ上）: 16件すべて成功。
- PostgreSQL 15（一時コンテナの専用スキーマ）: 同じ16件すべて成功。
- 既存のDB・Firebase認証情報はテストに使用していません。
- `git diff --check`: 成功。
