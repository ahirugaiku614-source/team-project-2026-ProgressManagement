# Stage 6：UDS収集サーバーと永続台帳

実装は専用worktree内だけです。Linux・Docker・PR #21環境に配置していません。
FastAPIの既定providerは拒否のままです。提出履歴GET APIはまだ未実装です。

## 配置承認後の起動順序（今回は実行しない）

1. 学生別/source、専用0700の/ledger、鍵/keys、管理socket用/rpcを新規に準備する。
2. 台帳の新規作成を承認された場合だけSQLiteReplayLedger.provisionを一度実行する。
   通常再起動ではprovisionを呼ばず、既存台帳をopenする。欠落時は停止する。
3. 管理側の信頼済みbootstrapからconfigured_serverへ固定設定・認可検証器を明示注入する。
4. 返ったUDSServerのserve(stop_requested)で待受する。停止条件は管理bootstrapが提供する。
5. 再起動時は旧プロセス停止とsocket実体を管理者が照合し、残存socketの除去を別承認する。

Composeの/ledgerは学生別の永続bind、/keysは読み取り専用bindとした。台帳/鍵/ソース/Git/DB
の包含・共有を非秘密設定の検査で拒否する。CLIは認可検証器未接続のため拒否のままです。

## 起動契約と信頼境界

UDSServerは固定パスにのみbindし、一接続ずつ直列処理します。待機backlogは1、
接続元UID/GIDをSO_PEERCREDでJSON解析前に確認します。親/対象のsymlink・所有者・
権限異常を拒否し、既存socketも自動削除しません。正常closeでも名前は残します。
再起動前のsocket除去は、管理者が同一性と旧プロセス停止を確認した別承認操作です。

既存HMAC・期限・ID・対象・世代・session契約を再利用します。クライアントは一要求送信後に
SHUT_WRし、サーバーは余剰データを拒否します。要求16KiB・応答15MiB、既定期限5秒。
送受信前後と処理前後で期限を確認し、期限超過の結果を返しません。同一プロセス内の
収集callbackやカーネルの停止したI/Oを強制中断する保証はありません。Linux実測と
独立ワーカーの強制期限設計は実接続前に判断する必要があります。

configured_serverへ管理者がendpoint、管理サービスUID/GID、学生別固定/source、対象、
所有UID、世代、既存/ledgerのパス、/keys内の方向別鍵ファイルを明示注入します。
鍵は0600の普通ファイルをO_NOFOLLOWで開き、同一性を確認し、32〜64byteだけ読みます。
authorize callbackは現在の認可と編集session/保存状態を検証する必要があります。
未指定・enabled未指定なら起動を拒否します。任意moduleやコマンドの動的ロードはありません。

FixedRootCollectorは管理設定のrootをFD相対で開き、登録済み対象だけをcollect_taskへ渡します。
Stage 3のリンク拒否・所有者・上限・二回照合を維持します。Git/DBを収集サービスに渡しません。
認可・dirty状態の真正性検証器自体は未解決です。現在のCLIはこの不足を隠さず拒否します。
構成案の学生別collectorは/source RO、/ledger RW、/keys RO、/rpc RWで、外部ネットワークなし。
実起動には信頼済みbootstrapの接続と別途承認が必要です。Composeを実行しただけで
動作するサービスや、本番への導入完了とは扱いません。

## 永続台帳

SQLiteReplayLedgerは学生から非公開の専用0700領域、0600ファイルを要求します。
初期化は明示provisionだけです。既存パスを上書きせず、通常openはmode=rwで欠落を拒否します。
BEGIN IMMEDIATEと一意キーINSERT、synchronous=FULLで収集前にACCEPTEDをcommitします。
再起動時にquick_check・スキーマ版・状態を確認し、使用済みIDを再利用しません。
状態はACCEPTED→COMPLETEまたはUNKNOWNです。COMPLETEは収集処理完了であり、
応答到達やGit/DB確定の証明ではありません。収集途中クラッシュのACCEPTEDも拒否を維持します。
書込み・ロック・破損・ファイル同一性異常は受付拒否を固定し、自動修復しません。
記録失敗を再帰的に記録せず、同じIDを再収集しません。保存内容は提出IDと状態だけです。

保持期間は現段階では無期限、自動削除なしです。将来の削除は「当該IDの恒久再利用拒否を
別の永続基準で維持」「Git/DB履歴と運用保持条件を確認」「管理者承認」を満たす場合だけ
別設計とします。台帳全体の整合した削除・巻戻しは外部基準なしでは検出保証できません。
SQLiteファイルをネットワークFSへ置かず、ジャーナルを含む一貫したバックアップを設計します。

## Linuxで必要な検証

- UID/GIDとuser namespace、ACL、学生A/B間の非公開境界、RO mountとbind mount境界。
- 実UDS・SO_PEERCRED、socket親/対象の差替え、既存socket残存、部分通信・期限・backlog。
- 実FD走査、symlink/hardlink/FIFO拒否、同時更新、収集callback停止時の処理。
- 複数プロセスSQLite競合・書込み失敗・容量不足・再起動・電源断、FULL同期の実効性。
- 鍵配置/交換、時計変動、実認可取消し、編集sessionとdirty状態の真正性。
- PostgreSQL・Gitとの整合復旧。提出履歴GETと教員権限は別工程。

Windowsでは一時SQLiteと偽socketのみで検証します。Linux固有の権限検査は偽物へ置換しており、
実環境成功とは扱いません。A2未完了、B1未承認、SSH255原因分類Hを維持します。

## Windowsでの検証結果

最終一括192件：185 PASS、0 FAIL、0 ERROR、7 SKIP。新規Stage 6は22 PASSです。
既存Stage 1〜5は163 PASS・7 SKIPを再確認しました。SKIPはLinux専用5件とPostgreSQL専用2件。
初回は破損時のSQLite初期化例外で接続が残り、一時ファイル解放が失敗しました。
製品側の初期化例外処理とテスト側の照会接続に明示closeを加えて再検証しました。
旧worktreeの7ファイルは既存バックアップと全件バイト一致、バックアップ10ファイル保持。
既存docker-compose.yml未変更、ステージ・commit・pushは未実施です。
