/** 未保存本文の取得を模擬する独立ライブラリ。拡張登録・通信・保存・実行は行わない。 */

export const MAX_CODE_BYTES = 64 * 1024;
const LANGUAGES = new Set(['python', 'c', 'cpp', 'java', 'javascript', 'typescript', 'csharp', 'go', 'rust']);

/** 入力値や元の例外を含まない固定エラー。本文・tokenをログへ反射させない。 */
export class BridgeRejected extends Error {
  constructor() { super('検証用の本文取得要求を受け付けられません。'); }
}

/** 本文と言語を検査する。許可するのは取得候補であり、実行対応言語の宣言ではない。 */
export function validateSnapshot(snapshot) {
  try {
    const keys = ['language', 'code', 'documentVersion', 'isDirty'];
    if (!snapshot || Object.keys(snapshot).length !== keys.length || keys.some(k => !Object.hasOwn(snapshot, k)) ||
        !LANGUAGES.has(snapshot.language) || typeof snapshot.code !== 'string' ||
        !snapshot.code.trim() || snapshot.code.includes('\0') || !snapshot.code.isWellFormed() ||
        new TextEncoder().encode(snapshot.code).length > MAX_CODE_BYTES ||
        !Number.isSafeInteger(snapshot.documentVersion) || snapshot.documentVersion < 1 ||
        typeof snapshot.isDirty !== 'boolean') throw new BridgeRejected();
    return Object.freeze({ language: snapshot.language, code: snapshot.code,
      documentVersion: snapshot.documentVersion, isDirty: snapshot.isDirty });
  } catch { throw new BridgeRejected(); }
}

/**
 * 明示操作時だけ、注入されたactive documentのgetText()を一度読む。
 * approvedDocument/rootPathは将来の信頼済みアダプターが決める値のモックである。
 * 字句上の範囲は検査するが、実ファイル・symlink・本人所有を実証する機能ではない。
 */
export function captureActiveEditor(editor, { explicitAction, approvedDocument, rootPath } = {}) {
  try {
    const document = editor?.document;
    const safePath = path => typeof path === 'string' && path.startsWith('/') &&
      !path.includes('\\') && !path.includes('\0') &&
      path.split('/').slice(1).every(part => part && part !== '.' && part !== '..');
    if (explicitAction !== true || !document || document !== approvedDocument ||
        document.isClosed !== false || document.uri?.scheme !== 'file' || document.uri.authority !== '' ||
        !safePath(rootPath) || !safePath(document.uri.path) || !document.uri.path.startsWith(rootPath + '/') ||
        !LANGUAGES.has(document.languageId)) throw new BridgeRejected();
    const version = document.version, language = document.languageId, dirty = document.isDirty;
    const code = document.getText(); // save()、fs.readFile、URIを指定した別文書の読取りは使わない。
    if (document.version !== version || document.languageId !== language || document.isDirty !== dirty ||
        editor.document !== document || document.isClosed !== false) throw new BridgeRejected();
    return validateSnapshot({ language, code, documentVersion: version, isDirty: dirty });
  } catch { throw new BridgeRejected(); }
}
