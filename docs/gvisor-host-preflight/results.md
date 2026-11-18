# 第14段階の検証結果と未実施事項

## 実施環境・結果

2026-11-18、Windows PowerShell＋開発用Python 3.14、新規worktree/branch `feat/gvisor-host-preflight`で実施。PR #15マージ済みmain `c980886b4fe907cbf693af1ae6a35d22bd43312e`を基点とする。新規ツールのLinux host調査モードは実行していない。

|検証|結果|
|---|---|
|新規チェックツール試験|37件成功（合成Linux/syscallモックと一時ファイルのみ）|
|既存＋新規の全体回帰|444件中282件成功、162件skip、失敗なし|
|PR #15のgvisor_lab.check|STATIC_CONSISTENT_ONLY、runtime_source_unchanged=true、実行/配備/接続false|
|事前ツールの既定CLI|collection/source=NOT_RUN、facts空、readiness=BLOCKED、実行/配備/接続false|
|Python mainの終了値|2をモック試験で確認。調査完了しても実行許可なしを表す意図した非ゼロ|

全体回帰は`PROJECT_TEST_DATABASE_URL`と`EDITOR_LAB_IMAGE_ID`を空にして、backendで次を実行した。

```powershell
python -B -W ignore::DeprecationWarning -m unittest discover -s tests -q
```

162件skipは従来と同じで、主にWindowsでは実行できないLinux Git/FS安全性試験、一時PostgreSQL未指定、opt-in実コンテナ試験。skipは成功件数へ含めない。既存Starlette TestClientの非推奨警告は残る。Composeの既存構文試験は偽設定で`docker compose config`するだけで、Engineへ接続・起動・再作成しない。

## 新規試験の内容

- 合成ARM64/x86_64、非root/不明OS、無効namespace、subuid/subgidの不足・重複・数値owner、NSSへ接続しない欠測処理。
- cgroup v1/hybrid、controller不足、静的な委譲/quota候補を実強制と扱わないこと。
- runsc ELF/digest/architecture不一致、バージョン・systrap未検証の維持、OS情報のshell非実行。
- 読取りFDのフラグ、親FDのclose、leaf symlink/FIFO/socket/device拒否、サイズ上限、NFS/autofsや重複mountの推測拒否。
- 既定ではprobeを構築しない、明示opt-inでも実行不可、例外/不明引数に秘密相当文字列を反射しない。
- import/callの静的検査でprocess/network client・書込みフラグを拒否。既存ゲート/レシピhash維持、未実施templateの検査項目網羅。

モックの`OBSERVED`は実機の観測ではない。`source=SYNTHETIC_MOCK`とし、すべての実行試験をNOT_RUNのままにする。

## NOT_RUNと今後の課題

専用Linux hostのOS/namespace/subuid/cgroup/実disk調査、runscとbundleの版/真正性、全process rootless、systrap、image build、Python/GCC/Node/Git実動作、CPU/RAM/pids/disk強制、実network/metadata遮断、本人ファイル隔離、停止後のdata保持、backup/restore、異常終了、実ブラウザ/HTTPS/Firebaseは**すべてNOT_RUN**。

読取りだけでは、上記の必須隔離要件は合格にできない。実executor・安全なパスresolver・保存集約器・sandbox用route・認証リレー・実境界での永続fencingは引き続き未実装。学校の準備と承認、限定診断の証拠、エディタ試験の開始条件を満たす別段階が必要である。

## 保護対象への影響

新規ファイルだけを追加。既存のCompose、volume、PostgreSQL/DDL、共有bare Git、workspace、KVM、gVisor参照アダプター、Firebase、HTTPS gateway、FastAPIを変更していない。既存エディタゲート3ファイルとレシピ4ファイルのhash検査を維持した。`.env`/秘密鍵/実利用者dataは読取り・テストに使用していない。

実Docker/runsc/VM、実DB、専用host、実Firebaseへ接続せず、実クラウドの契約・作成・課金・公開設定も行っていない。既存データの移動・削除・初期化・権限変更はない。mainへの直接コミットと自動マージを行わない。
