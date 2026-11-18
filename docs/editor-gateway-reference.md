# 第10段階：個人エディタHTTPSゲートウェイの参照実装

## 実装範囲と既存環境の保護

PR #11がマージされたmain `18ceb5e`から別worktree・新規ブランチで実装した。
`backend/editor_gateway/`は、HTTPサーバー/ソケットを持たない独立した認可ライブラリである。
FastAPIへのimport・ルーター登録・通信・公開は追加していない。
既定のAuthority/Directoryは必ず拒否する。環境変数で実接続へ切り替える仕組みもない。

| 区分 | 内容 |
| --- | --- |
| 実装済み | 本人/現在所属/VM所有者の照合契約、短期コード、S256照合、原子的な一回消費、固定期限セッション、CSRF/Host/Origin方針、期限付き接続状態機械 |
| モックで検証 | 失効/障害/VM世代変更、複数プロセス競合、仮想時刻での継続認可、Cookieヘッダー生成、秘密値の非表示 |
| 未実装 | 実Firebase/mTLS認可サービス、実管理サービス読取り、HTTPS/WSプロキシ、実ソケット切断、独立watchdog、TLS/DNS/証明書、ブラウザUI |
| 未検証 | 実機隔離・実ネットワーク分断・ブラウザCookie/SNI/HTTPヘッダー解釈・実code-server互換性・実時間の15秒切断 |

既存Compose、PostgreSQL、共有Git、workspace、VM管理レジストリ、Dockerボリュームには触れていない。
実VM・code-server・Docker Engine・SSH・libvirt・クラウドAPIを呼ばず、資格情報も読み込まない。
PR #9の実行無効API・WS全拒否・保護3ファイルのハッシュをそのまま維持した。

## クラス/関数と役割

各クラス・関数の先頭docstringに「何を確認する/何を作るか」を日本語で記載した。

| ファイル/主な要素 | 読み返す際の入口 |
| --- | --- |
| `contracts.py`: Authority / Identity | Firebaseの検証済み本人、失効、現在User/Team/Project照合の責務 |
| `contracts.py`: Directory / VMRoute | 高権限操作を持たず、本人VM/disk所有者・世代・準備証跡だけを読む契約 |
| `contracts.py`: RequestContext / 各Grant | 信頼済みHTTPアダプターからの要求情報、秘密値をreprへ出さない許可結果 |
| `store.py`: TestSessionStore | 新規一時SQLiteにハッシュ/一回消費/失効を保存。既存DBを上書きしない |
| `gateway.py`: Gateway.issue / exchange | 本人コード発行、verifier照合、消費とセッション作成 |
| `gateway.py`: Gateway.authorize / revoke | HTTP/WS共通の再認可、CSRF、世代照合、本人セッション失効 |
| `gateway.py`: check_context / cookie_header / upstream_headers | Host/Origin、Cookie属性、秘密ヘッダー中継禁止 |
| `connection.py`: ConnectionGuard | 長時間接続の期限監視、再認可、閉じた接続の再開禁止 |
| `tests/editor_gateway_fakes.py` | Firebase/所属/VMを模擬し、実接続なしで障害・失効を注入 |
| `tests/test_editor_gateway.py` | 一時DB、仮想時計、spawn子プロセスを使う回帰試験 |

## PR #9〜#11との対応

- **PR #9**：既存のFirebase UID→User→TeamMember→Project対応確認を信頼元とする。
  Gatewayがリクエスト本文のUser IDを採用するAPIは作らない。
  新しい参照コードを既存FastAPIへ接続せず、503/WS拒否と保護ハッシュを維持する。
- **PR #10**：[独立HTTPS設計](editor-foundation/architecture.md)の予約ドメイン、30秒コード、
  verifier、5分Cookie、15秒再認可、上流への資格情報非転送を具体化した。
  実DNS/TLSやネットワークは変更していない。
- **PR #11**：[VM管理](editor-vm-manager.md)のEditorBinding.environment_key、disk_keyと
  generationを再利用する。管理ロジックのソースは変更せず、起動/停止を呼ぶ権限も追加しない。
  PR #11のgenerationはVM再作成以外の操作でも増えるため、その場合も旧セッションを保守的に拒否する。
  これは接続を再認可する側の方針であり、既存generationの意味を変更しない。

VMRouteは将来の認証済み読取りサービスから得る契約で、RUNNINGだけでは接続可能としない。
VMとdisk両方の所有者、固定ID、正の整数generation、安全準備フラグ、有効期限15秒以内の証跡を要求する。
実サービスはguest TLS identity、外側ACL、health、未完了job等も検証する必要がある。
現在のモック証跡を実機の安全証明として採用してはならない。
ゲートウェイはlibvirt XML・ホストパス・起動/停止/削除/exec・任意上流URLを受け付けない。

## 本人確認と接続手順

1. 将来のアプリ画面が乱数verifierをブラウザメモリ内に保持し、そのS256 challengeを発行要求へ渡す。
2. `issue(id_token, project_id, challenge, context)`は正確なアプリOrigin/HostとPOSTを確認する。
   Authority.verify(check_revoked=True)の契約は署名、発行元/対象Firebase project、期限、失効・無効化を含む。
   Authority.resolveが現在のUID→User、TeamMember、Project所属からBindingを確定する。
3. 本人の接続可能VMを確認して、32ランダムbyte由来のコードを生成する。
   期限は30秒と元Firebaseトークン残存期限の短い方。コードのハッシュだけを保存する。
4. ブラウザがコードとverifierを**正確なエディタホストへのフォームPOST**で渡す。
   クエリ文字列、Referer、WS subprotocol、localStorageへ秘密値を入れない。
   cross-siteの交換では既存Cookieを認証に使わず、アプリの完全一致Originとverifierを確認する。
5. `exchange`は現在失効・所属・所有者・世代を再確認し、コード消費と新規セッション挿入を一つの
   BEGIN IMMEDIATEトランザクションで確定する。同じコードは別プロセスでも一度しか成功しない。
6. Cookie用sessionとCSRFをそれぞれ新規生成する。古いCookieをセッションIDに採用せず固定化を防ぐ。
   期限は交換時刻+5分と元Firebaseトークン期限の短い方。延長/refreshはない。
7. Cookieは以下の固定属性で発行する。Domainを設定する引数は設けない。

```text
__Host-editor=<ランダム値>; Path=/; Max-Age=<最大300>; Secure; HttpOnly; SameSite=Lax
```

`SessionGrant`は将来のHTTPアダプターだけに渡す内部結果で、公開JSON APIではない。
CSRF値は正確なエディタOrigin内の専用bootstrap等で渡し、アプリCORS・VM上流への転送は許可しない。
実bootstrap/Set-Cookie/キャッシュ抑止のHTTP応答は未実装。URI/body/access-logへの記録は禁止する。
Cookie属性は[MDN Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)、
verifierのS256は[RFC 7636](https://www.rfc-editor.org/rfc/rfc7636)を参照した。
本機能はOAuthフロー全体の実装ではなく、S256の方式を接続コードに使用する参照実装である。

## HTTP/WS共通認可と継続接続

`authorize`はCookieのハッシュから本人情報を引き、毎回失効・現在所属・VM証跡・generationを確認する。
Authority.check_sessionは保存したUID/auth_timeを使い、現在の無効化・失効時刻と照合する契約。
生のFirebase IDトークンは保持しない。既存DBとの接続も今回作らない。
[Firebaseの失効確認](https://firebase.google.com/docs/auth/admin/manage-sessions)と
[Python SDK](https://firebase.google.com/docs/reference/admin/python/firebase_admin.auth)が実アダプターの基準となる。
失効照会が応答しない場合も許可しない。照会後に削除が確定する競合まで遡って取り消す保証はない。

Host・TLS SNIは保存済みホストと完全一致、scheme=httpsを必須とする。
大文字、ポート付き、末尾ドット、サフィックス偽装は正規化せず拒否する保守的な試験方針。
通常の安全なGET/HEADはOrigin欠落を許容するが、Fetch Metadataはsame-origin/noneのみ。
WSと書込み系HTTPには正確なエディタOriginを要求する。
HTTP書込みにはセッション固有CSRFを追加照合する。WSは任意HTTPヘッダーを付けられないため、
Cookie＋完全一致Origin＋Fetch Metadataで開始前認可し、接続中も同じ認可を使う。
実ブラウザが送るヘッダーとの互換性は未検証。緩和せず、公開前の試験で判断する。

ConnectionGuardはWSと長時間HTTPストリームの両方で使うモデルである。
転送許可のdeadlineは**現在時刻+15秒、セッション絶対期限、VM証跡期限の最短**。
各方向のframe/chunkの転送直前にpermit_forwardを呼び、期限を過ぎたデータを通さない。
期限前（設計例10秒）にreauthorizeして、失敗なら閉じる。
照会が成功しても待機中に旧deadlineを越えたら閉じ、新許可で遡って復活させない。
一度閉じたguardは再開しない。セッション5分の絶対期限も延長しない。

**実時間15秒以内の切断はまだ保証していない。** 実relayは認可RPCとは独立したwatchdogを持ち、
idle中・RPC待機中もdeadlineでtickし、送受信両方向とソケットを閉じる必要がある。
現在はOSタイマー/ソケットを作らず、仮想時計でその契約を検証している。
実RPCの有界タイムアウト、イベントループ停止、壁時計ずれ、monotonicタイマー、バッファ済み出力の破棄、
ストリーム/WS/HTTP2の全経路の遮断は別途実装・検証が必要。未検証の間は公開不可。
WSのOriginと接続中のセッション照合は[OWASP WebSocket指針](https://cheatsheetseries.owasp.org/cheatsheets/WebSocket_Security_Cheat_Sheet.html)も参照した。

## 脅威・対策・残る制限

| 脅威 | 参照実装の対策 | 残る制限 |
| --- | --- | --- |
| 他人/他Projectのエディタ | UIDからの現在Binding、固定VM/disk/ホスト、所有者/世代の照合 | 実管理サービスの証跡署名/mTLSは未実装 |
| コード窃取/再利用 | verifier S256、30秒、一回消費、原子的挿入 | コードとverifierの両方の窃取/XSSは別対策が必要 |
| CSRF/WS乗っ取り | 正確なOrigin/Host/Fetch Metadata、書込みCSRF、WS開始前認可 | ブラウザ/HTTPパーサーをまだ試験していない |
| セッション固定 | 交換ごとに独立した乱数session/CSRF | Cookieが届く前に応答消失するとコードは消費済み。新規発行からやり直す |
| Cookie漏えい | Host限定・Secure・HttpOnly・Lax、保存はハッシュ、上流へ非転送 | Cookieはbearer。盗まれた有効Cookieの同一ホストでの再送は識別できない |
| VM応答によるCookie置換 | 上流Set-Cookie、Location、認証情報をallowlistから除外 | 実code-serverのヘッダー/拡張/リダイレクト互換性は未検証 |
| 認証基盤障害 | 新規認可拒否、継続許可の期限/再認可失敗で閉鎖 | 実ネットワーク障害/独立タイマーは未検証 |
| ログ/例外の秘密漏えい | ログ出力なし、固定例外、秘密フィールドrepr非表示、no-store/no-referrer方針 | デバッガのローカル変数/メモリダンプや依存先独自ログまで消去する保証はない |
| 時計逆行/DB失敗 | 永続last_msで拒否、消費と保存をロールバック | 信頼済みホスト時計が前提。NTP/停電/バックアップ復元は未検証 |

Cookieそのものの同一ホストでの再送は通常の複数HTTP/WS接続と区別できず、完全なリプレイ防止とは呼ばない。
窃取時は失効・短い期限で制限する。Cookie/コード/verifierをURLやログへ出さないことが重要。
本番ではcode-server側の任意JS/Service Workerも信用せず、アプリとは別の登録可能ドメインを維持する。
XSS対策、CSP、許可する上流リダイレクト、接続数・frame/帯域・idle制限、発行/交換のrate limit、
台帳の容量制限/掃除方針は未実装で、公開前に判断・検証する。汎用削除処理は今回追加しない。

RequestContextはTLS終端が作る信頼境界のDTOで、クライアントJSONの自己申告を認可するものではない。
将来のアダプターは重複Host/Origin/Cookie、曖昧なHTTP framing、偽Forwarded、
HTTP2 authorityとの不一致、query/subprotocol資格情報を入口で拒否する必要がある。
upstream_headersは最小allowlistの例で、TLSや完全なHTTPパーサーの代わりではない。

## 一時ストレージと障害復旧

試験用TestSessionStoreは専用の私有ローカル一時ディレクトリだけを使う。
新規作成はO_EXCLで上書きせず、再開は専用application_id/marker、通常ファイル・単一hardlinkを確認する。
親ディレクトリ差替え・特権攻撃者へのFS隔離を保証するものではない。
SQLiteのロックで同じファイルを使う複数プロセスを直列化する。別DBコピー/NFS/複数ホストは非対応。
本番の永続化/HA方式は決定しておらず、既存PostgreSQLにDDLを追加しない。

コード消費前の失敗はsessionを作らない。挿入途中のDBエラーは消費もロールバックする。
確定後の応答消失ではコードを再利用できないため、本人認証から新コードを発行する。
失効/消費済み記録を古いバックアップで巻き戻す復旧は禁止。台帳不明/破損は公開停止し、
全セッションを無効にする新しい権威/世代への切替方針を本番設計で決める。自動修復はない。

## 試験結果と再現

Windows / Python 3.14、モック・一時SQLite・専用spawn子プロセスのみで実施する。

```powershell
Set-Location <別worktree>\backend
$env:PROJECT_TEST_DATABASE_URL = ''
$env:EDITOR_LAB_IMAGE_ID = ''
python -m unittest discover -s tests -p test_editor_gateway.py -v
python -m unittest discover -s tests -v
python tools/editor_foundation_check.py
```

検証結果：**全331件、成功169件、スキップ162件、失敗0件**。

- 新規45件はすべて成功。4プロセスでのコード交換競合、所属削除、トークン/セッション失効、
  VM世代変更、ホスト台帳不整合、CSRF/Origin/Host、DB失敗時の原子的ロールバックを含む。
- RFC 7636の既知S256ベクトルに一致。秘密値は保存DBの平文、repr、標準出力、通常例外表示へ出ない。
- 継続WS/長時間HTTPは仮想時計で検証。旧期限を越える遅延応答、idle期限、失効後の閉鎖、
  5分期限の非延長を確認した。実ソケットでの15秒切断試験ではない。
- 既存286件は成功124件、スキップ162件。既存エディタAPIの実行不可、所属削除直後の拒否、
  VM管理38件を含む回帰が成功した。
- 第8段階checkerはdesign_consistent、runtime_source_unchanged=true。
  editor_launch_enabled / deployment_allowed / real_infrastructure_verifiedはすべてfalse、
  実機証跡23項目はNOT_RUNのまま。
- 既存FastAPI/Starlette TestClientの非推奨警告あり。依存パッケージは今回変更しない。

既存Composeの試験は合成値・空envのconfig解析だけでEngineに接続しない。
Linux専用Gitストレージ、一時PostgreSQL、opt-in実コンテナ試験は今回の環境/安全条件では実行しない。
実Firebase、実TLS、実VM、実WSは試験しておらず、モック成功を実配備の合格扱いにしない。
将来の有効化は[第8段階合否表](editor-foundation/verification.md)と
[導入手順](editor-foundation/runbook.md)を満たす別PRで行う。今回mainへの反映は行わない。
