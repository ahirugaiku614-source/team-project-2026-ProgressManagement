# Stage 7-A：管理側認可証拠と収集サービスの接続

実provider・実セッション検証器・CLIは既定拒否のまま。Windowsでのみ実装/検証する。
提出履歴GET APIは未実装。A2未完了、B1未承認、SSH255分類Hを維持する。

## 証拠と処理順序

RPC v1は従来の偽通信試験用として保持する。実収集bootstrapはv2を要求する
CollectorAuthorizationVerifierだけを受け付ける。v2はauthorizationを必須fieldとする。
証拠はversion、用途COLLECT_FOR_SUBMISSION、収集サービスID、要求全体binding、発行/期限、
署名からなる。bindingには学生・Team・Project・Task・登録相対パス・提出ID・request ID・
編集セッション・保存元世代・nonceが含まれる。RPC通信認証と別鍵・別ドメインのHMACを使う。
証拠は最大30秒で、提出許可とsessionの有効期限を超えない。全文や鍵はログに出さない。

発行側BackendAuthorizationIssuerは既存lock_target/resolve_submission_targetを再利用し、
Firebase依存関数で認証したUser、現在の所属/担当/登録対象、PROCESSING受付、消費済みpermit、
permitの全対応・期限、保存元世代、信頼済みsessionの保存状態を確認する。証拠発行は
許可の再消費やDB状態の再設定をしない。未認証入力からUserを組み立ててはいけない。

管理側factory authorized_rpc_providerへ既存Git store・RPC transport・永続ledger・session
検証器を明示注入する。FastAPI既定dependencyは変更しない。収集はRPCへ、Gitは管理側に残る。
収集側configured_serverは固定対象・世代に一致する認可検証器を要求する。収集側は
DBへ接続せず、署名・対象サービス・要求binding・期限・固定対象・世代・sessionを検証する。
接続元UID/GID→RPC認証→学生認可→永続台帳commit→収集→期限/認可再確認→応答の順を維持する。
失敗・並行・再起動時の再利用拒否はStage 6台帳を使用し、Git復旧でRPC再収集は行わない。

## sessionと失効の保証範囲

DeniedSessionVerifierは常に拒否する。VerifiedSessionは値型であり、生成しただけで
本物の証拠にはならない。実検証器は認証済みsessionと対象/世代/期限/dirtyなしを独立に
確認しなければならない。テストのFakeSessionsは実code-serverとは接続していない。
強制保存、バッファ送信、保存時の自動提出は追加しない。

収集側は発行後のTeam/担当変更をDBなしでは即時観測できない。短い証拠期限、収集前後の
session検査、管理側の前後DB認可でSUBMITTEDを防ぐ。既に読んだ内容を「未読」に戻す保証はない。
即時失効が必要なら認証付き失効照会または世代配信を別設計し、通信不明時は拒否する必要がある。

## 収集の実行期限

通信のsocket期限と、ファイル収集callbackの強制実行期限は別である。現実装は前後確認と
Stage 3の協調的期限で、停止中のOS I/Oを強制中断できない。この保証は未解決のままにする。
次の方式候補は、親プロセスが認可と永続受付を所有し、固定操作だけの隔離子ワーカーへ
認可済みroot/対象を渡す構成。ワーカーにはGit/DB/鍵を渡さず、出力をサイズ制限する。
期限時は当該子だけを終了・終了確認し、台帳をUNKNOWNとしてIDを再利用しない。
終了未確認もUNKNOWNで受付停止する。任意コマンドや学生コードを起動する方式にはしない。
今回はこのプロセス方式を実装/起動していない。Linuxで子の識別、終了・回収、FD継承、
停止したI/O、部分出力、再起動を合成試験しなければ、強制期限を保証しない。

## Stage 7-B前の条件

新規隔離root/UID/ACL/socket/台帳/鍵の固定、認証済みsession検証器と失効方式の確定、
実UDS/FD/SQLite並行性のLinux合成試験、強制期限ワーカーの設計承認が必要。
PR #21環境・学生データ・実DBを使わず、provider有効化と実配置は別途明示承認する。

## Windows検証記録

最終一括216件：209 PASS、0 FAIL、0 ERROR、7 SKIP。新規Stage 7-Aは24 PASS、
既存Stage 1〜6は185 PASS・7 SKIPを再確認した。SKIPは従来のLinux専用5件と
PostgreSQL専用2件。合成鍵・FakeSessions・偽socket・一時SQLite/Git・メモリDBだけを使用した。
実code-server、UDS、Linux権限、PostgreSQLの実証ではない。構文・差分検査もPASS。
旧worktreeの7ファイルは既存バックアップと全件バイト一致し、バックアップ10ファイルを保持。
commit・push・PR作成・実provider有効化・実環境操作は行っていない。
