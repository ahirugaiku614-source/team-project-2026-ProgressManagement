/** 検証専用コマンドの制御。本文はメモリ内だけで破棄し、他拡張へも返さない。 */
import { captureActiveEditor, BridgeRejected, MAX_CODE_BYTES } from '../capture.mjs';
import { approveFile } from './file-scope.mjs';

export const COMMAND = 'teamProgressProbe.capture';
const CONFIRM = '独立検証環境で取得する';

/**
 * 信頼済みVS Code APIに一つのコマンドだけを登録する。監視イベント・Webviewは作らない。
 * 本番用の認可ではなく、Linux/環境変数/専用root/モーダル確認による検証ゲートである。
 */
export function registerProbe(vscode, context, { env = process.env, platform = process.platform,
  uid = process.getuid?.(), euid = process.geteuid?.(), approve = approveFile } = {}) {
  const disposable = vscode.commands.registerCommand(COMMAND, async () => {
    let guard;
    try {
      if (env.TEAM_PROGRESS_EDITOR_PROBE !== 'ISOLATED_LAB_ONLY' || platform !== 'linux' ||
          !Number.isSafeInteger(uid) || uid <= 0 || euid !== uid || vscode.workspace.isTrusted !== true ||
          typeof ''.isWellFormed !== 'function') throw new BridgeRejected();
      const rootPath = env.TEAM_PROGRESS_EDITOR_PROBE_ROOT;
      const folders = vscode.workspace.workspaceFolders;
      if (!Array.isArray(folders) || folders.length !== 1 || folders[0].uri.scheme !== 'file' ||
          folders[0].uri.authority !== '' || folders[0].uri.path !== rootPath ||
          folders[0].uri.query || folders[0].uri.fragment) throw new BridgeRejected();
      const choice = await vscode.window.showWarningMessage(
        '本文は送信・保存・実行しません。承認済みの独立検証環境だけで使用してください。',
        { modal: true }, CONFIRM);
      if (choice !== CONFIRM) return;
      const editor = vscode.window.activeTextEditor, document = editor?.document;
      if (!document || document.isClosed !== false || document.uri.scheme !== 'file' || document.uri.authority !== '' ||
          document.uri.query || document.uri.fragment) throw new BridgeRejected();
      const filePath = document.uri.path;
      /** awaitの前後でworkspace/URI/信頼状態が変わっていないことを再確認する。 */
      const currentScope = () => vscode.workspace.isTrusted === true &&
        vscode.workspace.workspaceFolders?.length === 1 && vscode.workspace.workspaceFolders[0] === folders[0] &&
        vscode.workspace.getWorkspaceFolder(document.uri) === folders[0] &&
        vscode.window.activeTextEditor === editor && document.uri.scheme === 'file' &&
        document.uri.authority === '' && !document.uri.query && !document.uri.fragment &&
        document.uri.path === filePath && env.TEAM_PROGRESS_EDITOR_PROBE === 'ISOLATED_LAB_ONLY' &&
        env.TEAM_PROGRESS_EDITOR_PROBE_ROOT === rootPath;
      if (!currentScope()) throw new BridgeRejected();
      guard = await approve({ rootPath, filePath, uid, platform });
      if (!currentScope()) throw new BridgeRejected();
      // 本文全体を複製する前にUTF-16単位の過大文書を拒否する。
      // これはUTF-8 byte制限の代替ではなく、残りはcaptureActiveEditorで検査する。
      const offset = document.offsetAt(document.positionAt(MAX_CODE_BYTES + 1));
      if (!Number.isSafeInteger(offset) || offset < 0 || offset > MAX_CODE_BYTES) throw new BridgeRejected();
      const snapshot = captureActiveEditor(editor, { explicitAction: true, approvedDocument: document, rootPath });
      await guard.verify();
      if (!currentScope() || document.version !== snapshot.documentVersion ||
          document.languageId !== snapshot.language || document.isDirty !== snapshot.isDirty || document.isClosed) throw new BridgeRejected();
      // FDの閉鎖まで成功してから結果表示。本文・パス・hash・tokenは表示/ログ/返値へ出さない。
      await guard.close(); guard = null;
      await vscode.window.showInformationMessage(
        `本文取得の検証完了：${snapshot.language}／文書版${snapshot.documentVersion}／未保存${snapshot.isDirty ? 'あり' : 'なし'}。送信・保存・実行なし。`);
    } catch {
      await vscode.window.showWarningMessage('本文取得を拒否しました。検証環境・対象文書・所有権を確認してください。');
    } finally {
      if (guard) { try { await guard.close(); } catch {} }
    }
    // コード本文をcommandの返値にしない。他拡張からのexecuteCommandでも取得させない。
  });
  context.subscriptions.push(disposable);
}
