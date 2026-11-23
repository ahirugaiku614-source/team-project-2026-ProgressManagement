# PR #21：実エディター用本文取得プローブと検証手順

## 到達点

PR #20のmainへのマージを確認した。最新mainは
`f1b8ac54653aa875cd41825579e27d25058cafcc`、開始時の作業ツリーはclean。
`feature/code-server-real-editor-probe`で検証専用拡張とモックテストを追加した。

**初回実装時点では、拡張のソースとmanifestのみで実code-server検証はNOT_RUNだった。**

その後、利用者から実code-server上の固定テキスト照合について、正常系・不一致系の
手動確認結果が報告された。最新の限定結果と製品方針は末尾の「利用者による実機確認」
を参照。以下の過去日付のNOT_RUNやテスト集計は、当時の確認範囲として保持する。
既存ローカル環境を、承認済みの独立検証環境として扱う根拠がないため導入していない。
本文取得、通常ファイル/所有者/パス検査、失敗時拒否をモックで試験した。
学生プログラムの実行、Vueへの通信、FastAPIへの送信、Docker操作は実装しない。

## 既存環境から読み取れた情報（2026-11-20）

|対象|結果・限界|
|---|---|
|code-server CLI|WindowsのPATHから見つからなかった。Linux側の有無や版を否定するものではない|
|localhost:8080|既存の127.0.0.1待受を確認。認証なしHEADを1回だけ送りHTTP 302を受信。redirectを追わず、ログインもしていない|
|匿名HEADのframe関連header|CSP/frame-ancestors/X-Frame-Optionsはその応答では確認できなかった。認証後ページの制約がないという意味ではない|
|code-server/VS Code版|Dockerfileはcodercom/code-server:latest。実稼働版・digest・同梱VS Code版は未確認、NOT_RUN。上記HTTP応答だけではcode-serverであることも確定しない|
|Compose|127.0.0.1:8080、共通PASSWORD、単一code_server_workspace。ローカル用構成を維持し、User×Project分離の安全な個人環境とはみなさない|
|拡張host/Webview/iframe|対象環境の実行位置、実Origin、CSP、frame-ancestors等は未確認、NOT_RUN|
|既存gateway|認証・所有Binding・世代等の参照設計と実行無効ゲートを再確認。実プロキシへ接続しない|

Docker CLI、container exec、パスワード、Cookie、Firebase鍵、.envの値は使っていない。
HEAD応答のCookieやredirect先、認証情報の値は記録していない。
既存Docker/Composeの起動・再構築・設定変更は行っていない。

## 最小拡張の構成と用途

拡張rootは`labs/code-server-bridge/`。既存capture.mjsをそのまま同梱して再利用する。
拡張として配置しても、登録時にファイル読取・本文取得は行わない。

|ファイル|役割|
|---|---|
|package.json|Node/workspace側entry、コマンド1個、未信頼/仮想workspace非対応。自動onLanguage/onStartupは宣言しない|
|.vscodeignore|配布候補からテストと旧bridgeモックを除外する指定。VSIX自体は作成していない|
|probe/extension.cjs|VS Code APIを受け、controllerを読み込んでコマンド登録|
|probe/controller.mjs|検証ゲート、確認ダイアログ、文書/環境の前後照合、本文を返さない結果表示|
|probe/file-scope.mjs|読み取り専用のlstat/realpath/open/fstat。通常ファイル、UID、単一リンク、実体確認とFD閉鎖|
|capture.mjs（既存・未変更）|getTextを一度呼び、言語/本文/版/dirtyと64KiBを検査|
|probe.test.mjs|登録・取得・拒否経路と合成Linuxメタデータの試験|

コマンドは「検証専用：未保存本文をメモリ内で取得」だけ。実行には以下を全て要求する。

- 非rootのLinux。実UIDと実効UIDが同じ正の整数であること。
- プロセス環境の`TEAM_PROGRESS_EDITOR_PROBE=ISOLATED_LAB_ONLY`。
- `TEAM_PROGRESS_EDITOR_PROBE_ROOT`が、唯一のtrusted workspaceのfile URIと一致する絶対POSIXパス。
- そのrootと配下ディレクトリは本人UID所有でmode 0700相当。祖先はsymlinkでなく、
  rootまたは本人所有でgroup/other writableでないこと。通常ファイルは本人所有、nlink=1、
  group/other writableでないこと。同一root配下の別deviceも拒否する。
- 毎回のモーダル確認で「独立検証環境で取得する」を利用者が選ぶこと。

既定では環境変数がなく拒否する。環境変数は検証の誤作動防止であり、本人認証ではない。
Firebase token、Project ID、host URLや任意コマンドを受け取る設定はない。
manifestの最低VS Code API宣言は^1.90.0だが、対象hostでの互換性の実証ではない。
既存captureのString.isWellFormed/TextEncoder等が使えないruntimeでも拒否する。

## 取得とファイル検査

確認後のactiveTextEditor.documentの全文を、未保存変更を含めて一度取得する。
取得データはlanguage/code/documentVersion/isDirtyのみ。file URIのpathはメタデータ照合だけに
使用し、取得データ・ログ・コマンド返値・API要求へ含めない。ファイル内容はfsでは読まない。

本文の前に祖先・root・対象をlstat/realpathし、通常ファイルをO_RDONLY|O_NOFOLLOW|
O_NONBLOCKで開いてinode/owner等を照合する。本文取得後もFD、パス、祖先inode、workspace、
URI、文書版/言語/dirty、検証ゲートを再確認する。FD閉鎖を確認してから成功を表示する。
途中の失敗は固定の拒否表示になり、ファイルやworkspaceを削除・変更しない。
全文の複製前にpositionAt/offsetAtでUTF-16単位の過大文書を拒否し、取得後には既存captureで
UTF-8 byte上限を検査する。全文を無制限に複製してから制限する構成にはしない。

symlink、hardlink、特殊ファイル、他workspace、multi-root、untitled、外部scheme/authority、
query/fragment付きURI、root権限、不明runtimeは拒否する。save、terminal、compiler、
workspace.fs.readFile、HTTP、Webview、clipboard、OutputChannelは使わない。

成功表示は許可済み言語・文書版・dirty状態だけ。本文、path、hash、例外、認証情報は出さず、
コマンドの返値もundefined。取得snapshotは局所変数で、永続保持・送信せず処理終了時に破棄する。
取得後に利用者が編集したコードや未保存状態を上書きしない。

### セキュリティ上の限界

前後のメタデータ検査は、敵対的な同UIDプロセスによる全てのTOCTOUやmount差替えを
原子的に防ぐ仕組みではない。既にエディターにロードされているバッファの来歴を、後から
FD照合だけで証明することもできない。悪意ある他拡張・host管理者からの隔離も提供しない。

したがって**本番用User×Project認可や完全なファイル隔離が成立したとは判断しない**。
信頼済みの専用テストhost、秘密を持たない本人root、管理された拡張だけで使う。所有者・
実体パス確認が実機で成立しなければ停止する。mode不適合を理由に既存workspaceの権限を
一括変更しない。メタデータI/Oのハングを独立監視で中断する仕組みも未実装である。

## 専用Linux検証環境での手順（今回は未実行）

以下は環境担当者が別途承認した独立検証hostだけを対象とする。既存共有code-server、
Docker Desktopのvolume、アプリDB/Gitを使わない。新規VM・サービスを自動作成しない。

1. 検証host、非root UID、専用workspace/拡張dir/user-data/config-dirを担当者と確定する。
   他用途との共有があれば停止する。通常のcode-server、既存imageやvolumeは再利用しない。
2. そのhostで`code-server --version`とHelp/Aboutの表示を読み取り、code-server/VS Code版を
   記録する。拡張hostのNode版も担当者が確認する。CLIのNode版と同一とは推定しない。
   Developer: Show Running Extensions等でworkspace側Node hostに載るか確認する。
   token/Cookie/本文を含むDeveloper Toolsの全ログを収集・共有しない。
3. 提出コミットの拡張rootからmanifest、capture.mjs、probe/の3ファイル、READMEだけを
   オフラインでレビューする。コード/設定に秘密がないことを確認する。追加npm依存は不要。
4. **手動導入のみ**。担当者が用意した専用extensions-dirへ新しい拡張ディレクトリを作り、
   package.jsonとcapture.mjs、probe/extension.cjs/controller.mjs/file-scope.mjsを配置する。
   既存ディレクトリがあれば上書きせず停止する。既存code-serverへ--install-extensionしない。
   検証hostの導入方法が未確定ならここで停止し、自動インストールで代替しない。
5. 専用テストプロセスの環境だけに上記2変数を設定し、root値を新規のmode0700相当の
   専用workspaceへ一致させる。これは起動済みの既存プロセスを書き換える操作ではない。
   担当者が準備したテスト用認証とローカル接続だけを使用し、外部公開やauth noneを使わない。
   起動/再読込が必要なら、承認済みの専用プロセスについて担当者が実施する。
6. 新しい専用workspace内に合成fixtureを手動作成し、一度保存する。untitledは対象外。
   その後バッファだけを変更し、dirty表示を確認する。Pythonは例えば以下の文字列を使う。
   **これは編集データであり、ターミナルで実行しない。**

   ```text
   # PR21の合成fixture
   if True:
       probe_value = "未保存の確認"
   ```

7. コマンドパレットで取得コマンドを選び、モーダルを確認する。成功表示のlanguage/版/dirtyを
   記録する。本文をログやHTTPへ送らない。取得前後のバッファとdirty、保存済み内容が
   変わっていないことを画面と専用fixtureで確認する。CRLF/LFとインデントを別々に試す。
8. 下表を実施し、結果と対象コミットを記録する。言語modeが存在しない場合はNOT_RUNとし、
   未承認の言語拡張を追加インストールしない。現在の実行APIが対応したとは扱わない。

|試験|合格条件|
|---|---|
|Python未保存全文|取得完了、dirty維持。改行/インデント保持。自動保存なし|
|c/cpp/java/javascript/typescript/csharp/go/rust|各language modeで取得完了。compilerや実行プロセスは起動しない|
|64KiB／超過|UTF-8ちょうどは取得、超過は固定拒否。大きな合成テキストは専用fixtureで用意する|
|別workspace/外部URI/untitled|拒否。成功表示なし|
|symlink/hardlink/親directory symlink|専用fixture同士だけでリンクを作って拒否を確認。実ユーザーファイルへリンクしない|
|他UID/特殊ファイル/不適切mode|担当者が許可した合成fixtureでのみ確認。権限がなければNOT_RUN。sudoや既存データのchmodで試さない|
|認証情報・本文の非露出|拡張からconsole/OutputChannel/ファイル/通信へ出力しない。ネットワークや全ログに秘密を含む記録を残さない|
|確認キャンセル/環境変数なし|本文取得しない。成功表示なし|

想定外の成功、既存データへの接触、ログ漏えい、所有者/実体の照合不能があれば停止する。
後片付けは専用プローブを手動で無効にし、担当者が自分のテスト用接続/プロセスを閉じる。
一括削除・prune・volume削除・既存環境の停止は手順に含めない。fixture/拡張dirは担当者確認用に
残し、削除が必要なら新規作成した絶対パスと所有を別途確認して承認を得る。

## Vue連携方式の再評価

|方式|判断|
|---|---|
|Node/workspace拡張から直接|getText取得に適した候補だが親Vueのwindowを持たない。tokenを渡してAPI直行する代替は禁止|
|拡張→Webview|公式APIの範囲内だが、そこから親Vueへ安全に届くことは別問題。実Origin/CSP/iframeは未確認|
|postMessage/MessageChannel|Window参照・完全一致Origin/sourceと安全な初期port結合が必要。'*'やopaque Originで迂回しない|
|手動コピー→Vue貼付|自動bridgeなしの代替候補。出自の自動証明ができない制限がある。今回UIには追加しない|

今回の拡張はWebviewもネットワークも作らない。Vue/FastAPIへの送信は無効のまま。
実Origin/CSPが確認できるまで通信経路は実装しない。次PRは承認済み専用hostでの本書の
実機試験とheader/Origin確認を優先し、その結果でWebview bridge等の採否を決める。
Firebase tokenは拡張へ渡さず、POST /executeの認証・現在所属確認・503を維持する。

## 自動テストとNOT_RUN

Windowsの既存Node/Python依存だけを使用し、新規パッケージをインストールしない。

```powershell
# リポジトリ直下。実code-serverを起動するコマンドではない。
node --test labs/code-server-bridge/bridge.test.mjs labs/code-server-bridge/auth-logging.test.mjs labs/code-server-bridge/probe.test.mjs
```

Nodeは32 PASS、0 FAIL、0 SKIP（既存18＋新規14）。合成Linuxのfs/UID/flagsを注入した結果は
Linuxの実ファイル強制の実証ではない。既存取得/bridge/認証ログテストとの互換性を確認した。
バックエンドは561件中397 PASS、0 FAIL、164 SKIP（83.414秒）。
PROJECT_TEST_DATABASE_URL/EDITOR_LAB_IMAGE_IDを空にし、モック・一時SQLiteで実施した。
実Dockerを呼ぶtest_compose_config/test_editor_container_labの2モジュールは収集対象外で、
SKIPへ加算していない。SKIPは主にWindows非対応Linux Git/FS、隔離PostgreSQL未指定、
Linux専用worker3件。既定503と認証・所属確認を含む既存回帰を維持した。
合計593件中429 PASS、0 FAIL、164 SKIP。既存Starlette TestClientの警告は残る。

実code-serverの版/同梱VS Code/拡張ロード/未保存取得、実ブラウザーのWebview/Origin/CSP/
iframe、実Linuxでのsymlink/所有者検査、実Docker/gVisor/rootless、学生コード実行は全て
NOT_RUN。匿名HEAD一回の調査を、これらの成功と読み替えない。

## 追加確認：Ubuntu端末と既存SSH設定（2026-11-20）

以下は追加調査の記録であり、前項のモック試験を再実行した結果ではない。
実機拡張の9項目は引き続きNOT_RUN。VMへの拡張導入・起動・ディレクトリ作成はしていない。

|確認項目|区分・実施主体|確認結果|
|---|---|---|
|Ubuntu OS/CPU/実UID|PASS：利用者がUbuntu端末で読取り|Ubuntu 24.04.5 LTS、x86_64、非root UID 1000|
|Ubuntu側Git|PASS：利用者が読取り|feature/python-execution-worker、未コミット表示なし。git logで確定したHEADは8ce015b6de31e911b917bc7d03972f9c05294e76。PR #21のlabsファイルはこの作業ツリーに未取得|
|ホスト上のcode-server候補|調査済み：利用者が読取り|PATH、代表的な配置先、code-server/node/nodejs名の稼働プロセスからは未発見。別配置・Docker内の有無は断定しない|
|VirtualBox状態|PASS：Codexがhost metadataを読取り|Ubuntu分類の稼働VM候補を確認。NATの127.0.0.1:2222→guest:22。起動・設定変更・guest内操作なし|
|既存SSH設定|PASS：Codexが読取り|既存~/.ssh/configの127.0.0.1設定からユーザーと2222番を解決。対象設定と対応するVS Codeのpath/configFile上書きは確認されなかった|
|SSH非対話接続|FAIL：実接続試行|既存ホスト鍵の検証を維持してSSHサーバーへ到達したが、公開鍵認証を受理されずexit 255。パスワード入力は行っていない|
|既存認証の準備状況|調査済み：Codexのmetadata確認と利用者の説明|エージェントは起動中だが登録鍵0件。候補鍵ファイルは1件存在したが今回の公開鍵提示は受理されなかった。利用者の通常のRemote - SSH接続はパスワード入力方式|
|CodexによるUbuntu内部の追加調査|NOT_RUN|非対話認証未成立。SSHサーバーの故障や、利用者のパスワード接続も不可能という意味ではない|
|拡張ロード/コマンド表示/9言語取得/改行/自動保存防止/64KiB/領域外拒否/ログ|NOT_RUN|モック成功で代用しない。実code-server/VS Code/拡張host Nodeの版も未確認|
|認証後Origin/CSP/frame-ancestors/X-Frame-Options/iframe/Webview|NOT_RUN|ログイン後の実エディターへ到達していない。匿名HEADの結果を流用しない|

今回のSSH試行はBatchMode=yes、StrictHostKeyChecking=yes、UpdateHostKeys=noを使用し、
port forwardingと既存ControlMaster利用を無効にした一時的な呼出しである。
SSH設定、known_hosts、秘密鍵、authorized_keys、エージェントの登録内容は変更していない。
診断の本文は保存せず、認証成否の真偽値だけを確認した。パスワード、鍵本文、鍵のpath/指紋、
個人情報、認証Cookieを本書へ記録しない。

Codexから既存のVS Code Remote - SSHの対話セッションや入力済みパスワードを借用する
仕組みはない。パスワードをチャット、コマンド引数、環境変数、ログへ渡す代替は採用しない。
ホスト鍵検証を無効化したり、鍵追加・サービス起動・認証設定変更を勝手に行ったりもしない。
新しい認証手段の準備が必要なら、具体的な変更と影響範囲を示して別途承認を得る。

現在の障害は「Codexの非対話認証手段が成立していない」ことであり、VMが独立検証に
使えることやcode-serverの実機試験準備完了を確認できた、とは報告しない。
利用者からVMの利用承認は得ているが、環境を変更する操作の承認とは区別する。

### 追加変更なしで行える手動の読取り確認

必要であれば、利用者が既存Remote - SSHでログイン済みのUbuntu端末へ以下をまとめて
貼り付けられる。これは手動代替手順であり、CodexがSSH経由で実行済みという意味ではない。
インストール、起動、ファイル作成、Docker操作は含まない。

```bash
cat /etc/os-release
uname -m
id -u
if command -v code-server >/dev/null 2>&1; then
  printf 'code-server: PATH上に候補あり（版と動作は未確認）\n'
else
  printf 'code-server: PATH上では未発見\n'
fi
```

期待結果はOS/CPU/非rootの確認と候補有無だけ。候補がなければ版・拡張host検証はNOT_RUNで
停止し、apt/npm/Docker等による導入へ進まない。既存共有サービスを見つけても停止・再起動・
拡張導入はしない。次は認証と専用環境の準備について承認範囲を確定する必要がある。

## 公式資料（2026-11-20確認）

- [VS Code manifest](https://code.visualstudio.com/api/references/extension-manifest)
- [コマンドのactivation](https://code.visualstudio.com/api/references/activation-events)
- [TextDocumentとworkspace API](https://code.visualstudio.com/api/references/vscode-api)
- [Web extensionの制約](https://code.visualstudio.com/api/extension-guides/web-extensions)
- [Webview API](https://code.visualstudio.com/api/extension-guides/webview)
- [code-server FAQ](https://coder.com/docs/code-server/FAQ)

公式APIの一般仕様と、対象code-server版での動作・安全性の実証は分けて扱う。


## 固定テキストの未保存取得照合（機能と試験手順）

既存の独立Linux検証環境の制限と確認ダイアログを維持する。
追加の環境変数 TEAM_PROGRESS_EDITOR_PROBE_MODE を FIXED_TEXT_MATCH に
明示設定した場合だけ固定照合を行う。未設定なら従来の取得動作であり、
未知値・空文字は拒否する。照合失敗から通常動作へ切り替えない。

期待文字列は ASCII の TEAM_PROGRESS_UNSAVED_PROBE_V1 の直後に LF
(U+000A) を1個付けたものとする。UTF-8ではBOMなし。
CRLF、末尾LFなし、先頭BOMを同一視せず、文字列を厳密比較する。
これは比較用の固定データであり、ソースコードとして実行しない。

実機確認には別途承認が必要。許可済みworkspaceの通常ファイルを開き、
保存済み内容とは異なる上記文字列へ編集し、保存せず取得コマンドを実行する。
Untitledは使用しない。通知は MATCH/MISMATCH、取得時点のisDirty、
PASS/FAILだけ。MATCHかつisDirty=trueの場合だけPASSとする。
取得本文・本文断片・ハッシュを通知・console・ファイル・外部通信へ出さない。
比較結果は保存済みファイルとの照合証明ではないため、試験者が異なる
保存済み内容を用意する。以下は初回手順であり、その後の限定確認結果は次節を参照。

## 利用者による実機確認と今後の製品方針

記録元は利用者の手動確認報告である。実際のcode-serverで拡張コマンドを実行し、
確認ダイアログを承認して取得した次の2結果が報告された。
Codexが今回Linuxへ接続・起動・再実行して測定した結果ではない。
取得本文、ハッシュ、認証情報は記録しない。

|確認ケース|報告された通知|限定判定|
|---|---|---|
|未保存本文が固定テキストと異なる|`match=MISMATCH; isDirty=true; result=FAIL`|不一致系を確認済み。期待どおりのFAIL|
|未保存本文が固定テキストと一致する|`match=MATCH; isDirty=true; result=PASS`|正常系を確認済み|

利用者から別途共有された環境版はcode-server 4.145.2、内蔵VS Code 1.145.2。
実測日時と、当時導入されていた配布物のコミット・バイト同一性は未記録であり、
今回のステージ対象と同一だったと推測しない。この限定結果は、他の言語・境界値・
保存ファイル不変性・全セキュリティ条件の実機検証を代用しない。

以前の拒否警告については、Plain Textが許可言語に含まれないことが有力原因である。
Pythonへ言語モードを変更した後に取得できたとの報告がある。ただし、当該拒否の
再現・個別原因の切分けが完了したわけではなく、原因を完全確定とは記録しない。

今後の製品方式は、**学生が明示的に提出操作をしたときだけ、課題フォルダー内の
保存済みファイルをまとめて進捗管理システムへ提出する**ものとする。
自動保存・手動保存はどちらも許可するが、保存しただけでは提出扱いにしない。
未保存本文取得は検証成果として残し、製品の必須要件とはしない。
課題フォルダー提出のバックエンド基盤は別PRの対象であり、本PRの検証拡張へ
収集・送信・学生コード実行を追加するものではない。

本結果で別工程は完了にしない。A2は未完了、B1は未承認、SSH更新経路の255は
原因分類H（証拠不足で特定不可）を維持する。過去のUNKNOWN・OUTPUT_DETECTEDや
試験CのUNKNOWN・INCOMPLETE_RUN拒否も解除しない。

### 今回のマージ準備時の関連モック確認

HEAD `4a24a5f`に今回の検証用変更を加えたソースを対象に、以下のNode suiteを実行した。
過去の32件やバックエンド回帰の集計とは別の実行記録である。

- bridge.test.mjs：14件
- auth-logging.test.mjs：4件
- probe.test.mjs：28件（既存14件と固定照合の追加14件）
- 合計：発見46件、実行46件、PASS46件、FAIL0件、ERROR0件、SKIP0件、NOT_RUN0件。

上記はWindows上のモック確認であり、LinuxのFS強制や実code-serverの再測定ではない。
課題フォルダー提出基盤のPythonテスト、A2追加試験、実DB試験は今回実行していない。
