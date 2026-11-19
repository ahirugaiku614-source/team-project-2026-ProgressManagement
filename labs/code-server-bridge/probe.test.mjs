/** 実拡張の制御・メタデータ拒否をモックで検証する。VS Codeや学生コードは起動しない。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { registerProbe, COMMAND } from './probe/controller.mjs';
import { approveFile } from './probe/file-scope.mjs';
import { BridgeRejected } from './capture.mjs';

const ROOT = '/home/lab/work';
const FLAGS = { O_RDONLY: 0, O_NOFOLLOW: 0x20000, O_NONBLOCK: 0x800 };

/** Linux statとFDを模擬し、内容read/writeを一切持たないファイルシステムを作る。 */
function fileFixture() {
  const metadata = (ino, directory, uid = 1000, mode = directory ? 0o40700 : 0o100600) => ({
    dev: 1, ino, uid, mode, nlink: directory ? 2 : 1,
    isDirectory: () => directory, isFile: () => !directory, isSymbolicLink: () => false,
  });
  const rows = new Map([['/home', metadata(1, true, 0, 0o40755)],
    ['/home/lab', metadata(2, true)], [ROOT, metadata(3, true)], [ROOT + '/main.py', metadata(4, false)]]);
  const events = [], resolved = new Map();
  let closed = 0, opened;
  const io = {
    async lstat(path) { events.push(['lstat', path]); if (!rows.has(path)) throw new Error('SYNTHETIC_PATH'); return { ...rows.get(path) }; },
    async realpath(path) { return resolved.get(path) ?? path; },
    async open(path, flags) {
      assert.equal(flags, FLAGS.O_NOFOLLOW | FLAGS.O_NONBLOCK);
      events.push(['open', path]); opened = { ...rows.get(path) };
      return { stat: async () => ({ ...opened }), close: async () => { closed++; } };
    },
  };
  return { rows, resolved, events, io, closed: () => closed,
    approve: (changes = {}) => approveFile({ rootPath: ROOT, filePath: ROOT + '/main.py', uid: 1000, platform: 'linux', ...changes }, io, FLAGS) };
}

/** コマンド登録だけを行うVS Codeモック。環境証拠・所有者の本物の認証ではない。 */
function editorFixture(code = 'if True:\r\n    未保存 = 1\r\n') {
  const events = [], info = [], warnings = [], subscriptions = [];
  let reads = 0, command;
  const folder = { uri: { scheme: 'file', authority: '', path: ROOT } };
  const document = { uri: { scheme: 'file', authority: '', path: ROOT + '/main.py' },
    languageId: 'python', version: 4, isDirty: true, isClosed: false,
    getText() { events.push('getText'); reads++; return code; },
    positionAt: offset => ({ line: 0, character: Math.min(offset, code.length) }),
    offsetAt: position => position.character,
    save() { throw new Error('保存禁止'); } };
  const editor = { document };
  const api = {
    commands: { registerCommand(name, handler) { assert.equal(name, COMMAND); command = handler; return { dispose() {} }; } },
    workspace: { isTrusted: true, workspaceFolders: [folder], getWorkspaceFolder: () => folder },
    window: { activeTextEditor: editor,
      async showWarningMessage(message, options, choice) { warnings.push(message); return options?.modal ? choice : undefined; },
      async showInformationMessage(message) { events.push('info'); info.push(message); } },
  };
  const options = { env: { TEAM_PROGRESS_EDITOR_PROBE: 'ISOLATED_LAB_ONLY', TEAM_PROGRESS_EDITOR_PROBE_ROOT: ROOT },
    platform: 'linux', uid: 1000, euid: 1000,
    approve: async () => { events.push('approve'); return {
      verify: async () => { events.push('verify'); }, close: async () => { events.push('close'); },
    }; } };
  return { api, document, folder, options, events, info, warnings, reads: () => reads,
    register() { registerProbe(api, { subscriptions }, options); return command; } };
}

test('manifestは明示コマンドだけのNode/workspace拡張で、Webviewや自動起動を宣言しない', () => {
  const manifest = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'));
  assert.deepEqual(manifest.activationEvents, ['onCommand:' + COMMAND]);
  assert.deepEqual(manifest.extensionKind, ['workspace']);
  assert.equal(manifest.browser, undefined);
  assert.equal(manifest.capabilities.untrustedWorkspaces.supported, false);
  assert.equal(manifest.contributes.commands.length, 1);
});

test('拡張入口はロードだけでVS Code API・本文取得・通信を開始しない', () => {
  const require = createRequire(import.meta.url);
  const entry = require('./probe/extension.cjs');
  assert.equal(typeof entry.activate, 'function');
  assert.equal(typeof entry.deactivate, 'function');
  assert.equal(entry.deactivate(), undefined);
});

test('登録時は取得せず、明示コマンドと確認後に未保存本文を読み、返値へコードを出さない', async () => {
  const f = editorFixture(); const command = f.register();
  assert.equal(f.reads(), 0);
  assert.equal(await command(), undefined);
  assert.equal(f.reads(), 1);
  assert.equal(f.document.isDirty, true);
  assert.deepEqual(f.events, ['approve', 'getText', 'verify', 'close', 'info']);
  assert.equal(f.info.length, 1);
  assert.doesNotMatch(f.info[0], /未保存 = 1/);
});

test('9言語の本文を同じ取得処理で扱い、未知言語は固定拒否になる', async () => {
  for (const language of ['python', 'c', 'cpp', 'java', 'javascript', 'typescript', 'csharp', 'go', 'rust']) {
    const f = editorFixture(); f.document.languageId = language;
    await f.register()(); assert.equal(f.info.length, 1);
  }
  const f = editorFixture(); f.document.languageId = 'unknown';
  await f.register()(); assert.equal(f.reads(), 0); assert.equal(f.info.length, 0);
});

test('確認キャンセル、未許可環境、root、Windows、未信頼workspaceは本文を取得しない', async () => {
  for (const mode of ['cancel', 'disabled', 'root', 'effectiveRoot', 'windows', 'untrusted']) {
    const f = editorFixture();
    if (mode === 'cancel') f.api.window.showWarningMessage = async () => undefined;
    if (mode === 'disabled') f.options.env = {};
    if (mode === 'root') f.options.uid = 0;
    if (mode === 'effectiveRoot') f.options.euid = 0;
    if (mode === 'windows') f.options.platform = 'win32';
    if (mode === 'untrusted') f.api.workspace.isTrusted = false;
    await f.register()(); assert.equal(f.reads(), 0); assert.equal(f.info.length, 0);
  }
});

test('他workspace・multi-root・外部URI・untitled・閉じたdocumentを拒否する', async () => {
  for (const mode of ['foreign', 'multi', 'https', 'untitled', 'closed', 'authority', 'query']) {
    const f = editorFixture();
    if (mode === 'foreign') f.api.workspace.getWorkspaceFolder = () => ({});
    if (mode === 'multi') f.api.workspace.workspaceFolders.push({ uri: { ...f.folder.uri } });
    if (mode === 'https' || mode === 'untitled') f.document.uri.scheme = mode;
    if (mode === 'closed') f.document.isClosed = true;
    if (mode === 'authority') f.document.uri.authority = 'other-host';
    if (mode === 'query') f.document.uri.query = 'SYNTHETIC_SECRET';
    await f.register()(); assert.equal(f.reads(), 0); assert.equal(f.info.length, 0);
  }
});

test('64KiBちょうどを受理し、byte超過では本文や例外を表示しない', async () => {
  const text = '#' + 'あ'.repeat(21845);
  const f = editorFixture(text); await f.register()(); assert.equal(f.info.length, 1);
  const tooLarge = editorFixture(text + 'SYNTHETIC_SECRET');
  await tooLarge.register()(); assert.equal(tooLarge.info.length, 0);
  assert.doesNotMatch(JSON.stringify(tooLarge.warnings), /SYNTHETIC_SECRET/);
  const huge = editorFixture('a'.repeat(65537));
  await huge.register()(); assert.equal(huge.reads(), 0); assert.equal(huge.info.length, 0);
});

test('前後の実体確認失敗と文書切替では採用せず、FDを閉じてrawエラーを隠す', async () => {
  for (const mode of ['approve', 'verify', 'switch', 'close']) {
    const f = editorFixture('SYNTHETIC_SOURCE');
    if (mode === 'approve') f.options.approve = async () => { throw new Error('SYNTHETIC_SECRET'); };
    else f.options.approve = async () => ({
      verify: async () => {
        if (mode === 'verify') throw new Error('SYNTHETIC_SECRET');
        if (mode === 'switch') f.api.window.activeTextEditor = {};
      },
      close: async () => { f.events.push('close'); if (mode === 'close') throw new Error('SYNTHETIC_SECRET'); },
    });
    await f.register()(); assert.equal(f.info.length, 0);
    if (mode !== 'approve') assert.ok(f.events.includes('close'));
    assert.doesNotMatch(JSON.stringify(f.warnings), /SYNTHETIC_SECRET|SYNTHETIC_SOURCE/);
  }
});

test('通常ファイルだけをNOFOLLOWで開き、内容を読まず前後照合してFDを閉じる（合成Linux）', async () => {
  const f = fileFixture(); const guard = await f.approve();
  await guard.verify(); await guard.close();
  assert.equal(f.closed(), 1);
  assert.equal(f.events.filter(([name]) => name === 'open').length, 1);
});

test('symlink・hardlink・特殊ファイル・他所有者・危険な権限・別deviceを拒否する（合成Linux）', async () => {
  for (const mode of ['symlink', 'hardlink', 'special', 'owner', 'writable', 'device', 'parentLink', 'rootShared', 'unsafeInode']) {
    const f = fileFixture(), file = f.rows.get(ROOT + '/main.py');
    if (mode === 'symlink') file.isSymbolicLink = () => true;
    if (mode === 'hardlink') file.nlink = 2;
    if (mode === 'special') file.isFile = () => false;
    if (mode === 'owner') file.uid = 9999;
    if (mode === 'writable') file.mode |= 0o002;
    if (mode === 'device') file.dev = 2;
    if (mode === 'parentLink') f.rows.get('/home/lab').isSymbolicLink = () => true;
    if (mode === 'rootShared') f.rows.get(ROOT).mode |= 0o050;
    if (mode === 'unsafeInode') file.ino = Number.MAX_SAFE_INTEGER + 1;
    await assert.rejects(f.approve(), BridgeRejected);
    assert.equal(f.events.filter(([name]) => name === 'open').length, 0);
  }
});

test('realpathの領域外解決・不正root・他workspaceのパスを拒否する（合成Linux）', async () => {
  const f = fileFixture(); f.resolved.set(ROOT + '/main.py', '/outside/main.py');
  await assert.rejects(f.approve(), BridgeRejected);
  for (const filePath of ['/home/lab/work-other/main.py', ROOT + '/../main.py', ROOT + '/a//main.py']) {
    await assert.rejects(fileFixture().approve({ filePath }), BridgeRejected);
  }
  await assert.rejects(fileFixture().approve({ rootPath: '/' }), BridgeRejected);
});

test('open後のinode交換・root交換・リンク数変更を検出してFDを保持したまま成功にしない（合成Linux）', async () => {
  for (const mode of ['file', 'root', 'link']) {
    const f = fileFixture(); const guard = await f.approve();
    if (mode === 'file') f.rows.get(ROOT + '/main.py').ino++;
    if (mode === 'root') f.rows.get(ROOT).ino++;
    if (mode === 'link') f.rows.get(ROOT + '/main.py').nlink++;
    await assert.rejects(guard.verify(), BridgeRejected);
    await guard.close(); assert.equal(f.closed(), 1);
  }
});

test('root・非Linux・NOFOLLOW未提供・rawファイルエラーでは失敗側に閉じる', async () => {
  for (const changes of [{ uid: 0 }, { platform: 'win32' }]) await assert.rejects(fileFixture().approve(changes), BridgeRejected);
  const f = fileFixture();
  await assert.rejects(approveFile({ rootPath: ROOT, filePath: ROOT + '/main.py', uid: 1000, platform: 'linux' }, f.io, {}), BridgeRejected);
  f.io.lstat = async () => { throw new Error('SYNTHETIC_SECRET_PATH'); };
  await assert.rejects(f.approve(), error => error instanceof BridgeRejected && !String(error).includes('SYNTHETIC_SECRET'));
});

test('実体検査中のworkspace変更・URI差替え・信頼取消しも本文取得前に拒否する', async () => {
  for (const mode of ['folder', 'uri', 'trust']) {
    const f = editorFixture();
    f.options.approve = async () => {
      if (mode === 'folder') f.api.workspace.workspaceFolders = [];
      if (mode === 'uri') f.document.uri.path = ROOT + '/other.py';
      if (mode === 'trust') f.api.workspace.isTrusted = false;
      return { verify: async () => {}, close: async () => { f.events.push('close'); } };
    };
    await f.register()(); assert.equal(f.reads(), 0); assert.equal(f.info.length, 0);
    assert.ok(f.events.includes('close'));
  }
});
