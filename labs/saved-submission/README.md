# 課題提出・新規隔離環境の構成案（Stage 5）

Stage 6でUDS待受ロジック・明示bootstrap・永続SQLite台帳を追加しました。
以下の「待受・台帳未実装」はStage 5時点の記録です。現在の契約・起動条件は
`../../docs/task-submission-stage6.md`を参照してください。実接続の既定拒否は維持しています。

この領域は構成案と合成データだけであり、導入・起動承認ではありません。
既存docker-compose.yml、PR #21、旧worktree、バックアップは使用・変更しません。
この工程ではDocker Composeを実行していません。全サービスは明示profile配下です。

## 配置とIDの承認条件

- student-aとstudent-bを学生×Project単位に分離します。複数Projectならインスタンスも分けます。
- ホストから見える学生A/B・収集A/B・管理サービス・DBのUID/GIDはすべて別の非root値とします。
  コンテナ内の数値だけで同一性を決めず、user namespace/rootlessのマッピングを確認します。
- 各学生は自分のworkspaceだけRWで参照します。Git、DB、管理socket、Docker socketは渡しません。
- 各収集サービスは対応学生のProjectだけROで参照し、他学生の領域はマウントしません。
  socket領域だけ別の書込み先を持ち、network_mode=none、外部portなしとします。
- 管理サービスだけがGit保存領域とDBへアクセスします。学生A/B/管理ネットワークは別です。
- ソース・socket・Git・DB・設定は承認された新規root配下で互いに包含しない独立パスとします。
  bindのcreate_host_pathはfalseです。存在しない元パスを自動作成しません。
- backend/app/submission_lab_plan.pyの純粋な検査で非秘密設定を確認します。秘密値は渡しません。
  検査成功は実パス、symlink、ACL、mount、空き容量の確認を代替しません。
- imageはdigest固定を要求します。既存イメージのlatestを無条件に使用しません。

Composeの必須変数には既定UID/GID・パス・秘密を埋め込んでいません。A/B認証configは
管理者が別途用意する新規ファイルです。auth=noneは使用しません。manager/DBのパスワードは
起動時の外部注入を想定した案ですが、環境変数は管理者のinspectから見え得ます。
本番前にはsecretファイル等からの読込み対応と権限を別途レビューします。
image内に読み取り専用の提出用拡張を含める方法・ホームディレクトリの書込み先・DB imageの
実UIDと初期化方法も未確定です。現在の構成案を本番用・起動確認済みとは扱いません。

## 内部RPCの契約

submission_rpc.pyは待受を持たない処理器とクライアントです。要求は4-byte big-endian長と
厳密UTF-8 JSONの一フレーム、最大16KiB。応答は最大15MiB（10MiB本文のBase64等を含む）です。
重複JSONキー、未知field、余剰フレーム、途中EOF、不正型、過大データを拒否します。

要求は版・固定操作・提出ID・request ID・編集セッションID・認可済み対象・保存元世代・nonce・
発行/期限・固定サービスIDだけです。絶対パス・任意URL・APIの提出許可原文は渡しません。
既存の相対パス検査を共有し、登録済み対象と世代の完全一致を要求します。
DB非依存のsubmission_scope.pyに値型を分離し、収集サービスにはDB資格情報を要求しません。

方向別の異なる鍵を使うHMAC-SHA256で双方を認証し、応答を要求全体に束縛します。鍵は32byte以上。
鍵値・本文・例外全文は記録せず、鍵のreprも隠します。HMACは暗号化ではありません。
実同一ホスト接続はUDS権限とSO_PEERCREDを併用し、別ホストでは相互TLS等が別途必要です。
鍵の配布・権限・交換、時計同期、失効確認は配置前の承認事項です。

既定transportは拒否です。UnixSocketTransportもenabled=Falseが既定で、無効時はlstatや
socket作成をしません。明示注入した場合だけLinux UDSを使い、親/対象metadata、接続相手UID、
全体期限、受信中サイズ、完全EOFを検査します。実UDSのクライアントは実装しましたが、
サーバーの実待受・SO_PEERCRED検査・鍵読込み・永続台帳を組み合わせた起動処理は未実装です。
Composeのcollectorコマンドは現在拒否して終了します。設定だけで実接続へ切り替わりません。

MemoryReplayLedgerはテスト専用の一回受付です。収集前に提出IDを消費し、並行/重複/応答喪失でも
再送しません。再起動後の再利用拒否は保証しないため、実接続前には永続的で排他的な台帳が必須です。
台帳の欠落・破損時は新規受付を拒否する契約で実装する必要があります。

## 既存providerとの接続

テストでCollectorRPCClient.collectをManagedSnapshotProviderのcollector引数へ注入します。
収集は内部RPC、GitSnapshotStoreは管理側に残します。collector未指定ならStage 3の従来sourceを
そのまま使用します。FastAPIのget_submission_providerは変更せず、実接続を常に拒否します。
RPC失敗・Git保存失敗では同一提出IDを再収集しません。復旧はStage 3のGit証拠照合だけです。

## Linux限定検証のチェックリスト（未実施）

1. 新規rootが未使用・非symlinkで、PR #21等と包含/同一実体でないこと。
2. UID/GIDとrootless/usernsの対応、学生A/B間のアクセス拒否、学生の管理領域アクセス拒否。
3. ACLは必要な収集者だけに読取り/走査を許可。group/other書込みは許可しないこと。
4. RO mountの書込み拒否、既存submount/同一device bind mountの検出、伝播rprivate。
   st_dev一致だけではbind mountを排除できないため、mount ID等の追加検査を確定すること。
5. 合成ファイルだけでsymlink/hardlink/FIFO拒否、読取り中の変更、サイズ/件数/深さ超過。
6. socket親・socket所有者/ACL、SO_PEERCRED、悪い相手UID、差替え、余剰/部分/巨大フレーム。
7. 期限切れ、時計変動、途中切断、二重/並行要求、再起動後の永続台帳で再利用拒否。
8. 本文・認証情報がログに出ないこと。パケットを第三者サービスへ転送しないこと。
9. Git保存失敗、DB確定失敗、既存commitからの復旧、PostgreSQL並行制御。
10. dirty状態を別ウィンドウまで真正に確認する仕組み。現時点では未解決。

合成fixtureはfixtures/以下に学生A/B・課題別に用意しています。リンクやmountはこの工程で
作成していません。実学生データをコピーしてfixtureにすることは禁止です。
既存の123 PASS/7 SKIPは履歴です。A2未完了、B1未承認、SSH255分類Hを維持します。

## Stage 5のWindows検証結果

新規40 PASS、既存Stage 1〜3と関連API123 PASS、計163 PASS・0 FAIL・0 ERROR・7 SKIP。
一括170件の後、DBを含む全6主体の非root/ID分離設定に合わせ、構成テスト6件を限定再確認し
6 PASSでした。新規の検査に実socket、Docker、SSH、実DB、学生データは使用していません。
SKIPは既存のLinux専用5件・PostgreSQL専用2件です。
初回の偽UDS試験ではWindowsにないAF_UNIX定数の注入不足が1 ERRORとなりましたが、
偽定数を追加し再検証しました。この履歴をLinuxの実UDS成功と読み替えません。
既存環境にあったPyYAML 6.0.3をrequirements-dev.txtへ明示し、ComposeはYAMLとしてのみ解析しました。
