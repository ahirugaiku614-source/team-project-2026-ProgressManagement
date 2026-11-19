# 学生向けPython実行API：第1段階

追記：別process向けworkerのDraft実装を[python-execution-worker.md](python-execution-worker.md)へ追加した。同一hostの専用SQLiteで共有4枠を管理するロジックはモック試験済みだが、API→workerの実通信と実Engine接続は未実装。以下の第1段階APIは引き続き既定503である。

今回はFastAPIと未接続worker契約、モックテストだけを追加する。学生コードをAPI内のexec/eval/subprocessで実行せず、Docker/runsc・実worker・クラウドへ接続しない。既定では常に503となり、有効化する環境変数・Docker socket・Docker SDK・sudo設定は存在しない。

## API

`POST /execute`、`Authorization: Bearer <Firebase ID token>`。既存の失効確認付きFirebase検証を再利用し、既存のTeam所属・Project対応確認関数を通す。UserはFirebase UID→DBのUserから取得する。本文のuser_id/student_idは許可しない。

```json
{
  "team_id": 1,
  "project_id": 2,
  "language": "python",
  "code": "print('Hello')\n"
}
```

team_id/project_idは正の32bit整数。languageはpythonのみ（省略時もpython）。codeはUTF-8で最大65,536 bytes。空、空白だけ、NUL、不正Unicode、不正型、未知フィールドは拒否する。インデントや末尾改行は変更しない。構文検査・実行は行わず、SyntaxError相当もworker結果のerrorとして扱う。

モック正常応答の例：

```json
{
  "status": "completed",
  "stdout": "Hello\n",
  "stderr": "",
  "exit_code": 0,
  "execution_time_ms": 12
}
```

|status|意味|exit_code|
|---|---|---|
|completed|workerが正常終了として返した結果|0|
|error|workerが実行失敗として返した結果|非0、または確定不能ならnull|
|timeout|workerが実行期限切れとして返した結果|null|

これらの実行結果はHTTP 200。APIが実行時間・終了コードを生成するのではなく、モックworker値を検査する。stdout/stderrはそれぞれUTF-8最大65,536 bytes。実行時間は非負整数であり、今回5秒の実行期限を強制する処理はない。5,000msは将来のworker契約値である。表示側は出力をHTMLとして解釈せず、plain textとして扱う必要がある。

|HTTP|内容|
|---|---|
|401/403|未認証（HTTPBearerの拒否を含む）、不正/失効トークン、認証障害|
|403|現在のTeam非所属|
|404|User/Team/Project不在、ProjectとTeamの不一致|
|400/422|JSON/UTF-8解析失敗、入力不正。コード・token等は検証エラーへ反射しない|
|429|このprocessの共有クライアントが4件受付中。待機列なし、Retry-After: 1|
|503|既定の未接続worker、worker例外、不正・別実行・別所有者の応答|

HTTPエラーは固定detailを返し、上の実行結果形式とは区別する。通信例外を、実行がtimeoutしたという結果へ推測で変換しない。

## workerの境界

`execution_client.py`の`ExecutionWorker.execute()`に、APIが生成したexecution_id、認可済みEditorBinding、Python code、RUN_PYTHON操作と固定policyを渡す。共有Binding型は読取り再利用するが、個人エディタの実装やゲートは変更しない。

policyはCPU 500 milliCPU、RAM 128MiB、network none、runtime runsc、実行期限5,000msで固定する。image、host path、任意URL、Docker設定、自由なexec操作を指定する契約はない。実Dockerへの変換・資源制限の強制は未実装。返信もexecution_idとUser/Team/Projectを照合し、不正な出力・矛盾したstatus/exit_codeを503へ落とす。

既定の`DisconnectedWorker`は必ず拒否する。テストだけがFastAPIのdependency_overridesで固定結果workerを注入する。Pythonコードは文字列として渡されるだけで、テストでも学生コードを評価しない。

## 4件制限の保証範囲

module共有ExecutionClientのBoundedSemaphore(4)で、**同一process・同一clientの同期呼出し**を4件まで受け付ける。4件をモックで待機させ、5件目がHTTP 429となることを検査する。呼出しが完了/例外終了するとAPI受付枠をfinallyで戻す。

これは実コンテナ枠ではない。別Draftのworkerには同じ専用SQLiteを使う原子的予約管理を追加したが、APIの実接続はない。複数host/別DB間の予約は未実装。実workerへの要求が不明なまま途切れた際に、このAPI枠解放をコンテナ停止の証明にしてはいけない。実接続・権限・UNKNOWN復旧の実機検証が整うまで503を維持する。

個人エディタのVM4台上限とは別の試験契約で、既存上限を変更していない。将来同じhostで併用するなら、合算資源予算・共通予約方針を決める必要がある。

## テストと既存への影響

`test_execution_api.py`は独立SQLiteと合成User/Team/Projectを使用する。外部Firebase検証だけをmockし、Bearer・現在所属・Project対応の実処理を通す。`test_execution_client.py`は固定結果・Event待機のthreadを使い、実Pythonプログラム・Dockerを起動しない。

```powershell
# backendで既存の開発用Pythonを使用。新しいpackageは不要。
python -B -W ignore::DeprecationWarning -m unittest discover -s tests -p 'test_execution_*.py' -v
```

新規28件はPASS。初回の検査では不正UTF-8の標準HTTP 400を422と限定したテストが1件失敗したが、APIの解析拒否は正常で、400/422を許容する試験へ修正して全件成功した。

Dockerを使わない回帰では`test_compose_config`と`test_editor_container_lab`の2moduleを除外し、470件中309件PASS・161件skip・FAIL 0件。PROJECT_TEST_DATABASE_URLとEDITOR_LAB_IMAGE_IDは空にして実行した。skipは主にLinux専用Git/FS試験と未指定の隔離PostgreSQL試験。既存のStarlette TestClient非推奨警告は残る。実Docker CLI/Engine、実gVisor、実Firebase、既存DBは使用せず、依存の追加インストールも行っていない。

第1段階ではmain.pyへrouter登録、schemas.pyへ独立型を追加し、commit/push/mergeを行わなかった。その後の依頼で7ファイルのリポジトリ外backupとSHA256照合を行い、専用ブランチへ先に保存した。worker追加も別commitとDraft PRでレビューする。既存DBモデル・DDL・Docker/依存設定・KVM/個人エディタ・Firebase実装は変更しない。

## 次の段階で必要なこと

専用Linuxワーカーの最小権限、rootless/runscの実証跡、固定digestのPython専用image、非root・capability削除・read-only root・ネットワーク遮断、pids/disk/tmpfs/swap制限を別に設計・検証する。CPU 0.5/RAM 128MiBは性能確認済みではない。

入力のHTTP body全体上限（JSON解析前）、出力ストリームの逐次上限、wall-clockの5秒監視と実行環境全体の停止、切断/キャンセル、永続/分散予約・冪等性・未知状態の復旧、利用者ごとのrate limitも未実装。今回のPython str上限や返却値の検査は、その代わりにならない。

実通信を追加する際はworker認証・本人/Projectの再認可・秘密情報非露出を確認し、未知状態で実行を許可しない。実イメージのbuild、コンテナ起動、既存環境への反映は今回実施していない。
