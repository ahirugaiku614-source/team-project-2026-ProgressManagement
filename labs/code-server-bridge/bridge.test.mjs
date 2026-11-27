/** Node組込みtestによるモック検証。実ブラウザー・code-server・学生コードは実行しない。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { captureActiveEditor, BridgeRejected, MAX_CODE_BYTES } from './capture.mjs';
import { MockBridge } from './bridge.mjs';

/** VS Codeの読み取りAPIだけを持つdocumentを作る。save/fs/terminalの呼出しは失敗させる。 */
function documentFixture(code = 'if True:\r\n    未保存 = 1\r\n') {
  let reads = 0;
  const document = { uri: { scheme: 'file', authority: '', path: '/workspace/main.py' },
    languageId: 'python', version: 3, isDirty: true, isClosed: false,
    getText() { reads++; return code; }, save() { throw new Error('保存禁止'); } };
  const editor = { document };
  return { document, editor, reads: () => reads,
    capture: () => captureActiveEditor(editor, { explicitAction: true, approvedDocument: document, rootPath: '/workspace' }) };
}

/** 予約済み.test Originとopaqueなsource参照を作り、実windowを使わず返信を合成する。 */
function bridgeFixture() {
  const owner = { teamId: 1, projectId: 2, userId: 3 }, source = {};
  let time = 100;
  const bridge = new MockBridge({ labMode: true, owner, source, origin: 'https://editor.example.test',
    generation: 7, clock: () => time, nonce: () => '11111111-1111-4111-8111-111111111111' });
  const request = bridge.request({ explicitAction: true, owner, generation: 7 });
  const event = { origin: 'https://editor.example.test', source, data: { type: 'SNAPSHOT', nonce: request.nonce,
    owner, generation: 7, snapshot: documentFixture().capture() } };
  return { bridge, owner, event, current: { owner, generation: 7 }, advance: delta => { time += delta; } };
}

test('明示操作で未保存本文全体を一度だけ取得し、インデント・改行とdirty状態を保持する', () => {
  const fixture = documentFixture();
  const snapshot = fixture.capture();
  assert.equal(snapshot.code, 'if True:\r\n    未保存 = 1\r\n');
  assert.equal(snapshot.isDirty, true);
  assert.equal(fixture.document.isDirty, true);
  assert.equal(fixture.reads(), 1);
  assert.equal(Object.hasOwn(snapshot, 'path'), false);
});

test('自動取得・未承認document・外部workspace/URIは本文を読む前に拒否する', () => {
  for (const change of ['implicit', 'otherDocument', 'otherRoot', 'uri', 'authority', 'traversal', 'closed']) {
    const f = documentFixture(), options = { explicitAction: true, approvedDocument: f.document, rootPath: '/workspace' };
    if (change === 'implicit') options.explicitAction = false;
    if (change === 'otherDocument') options.approvedDocument = {};
    if (change === 'otherRoot') f.document.uri.path = '/workspace-other/main.py';
    if (change === 'uri') f.document.uri.scheme = 'https';
    if (change === 'authority') f.document.uri.authority = 'remote';
    if (change === 'traversal') f.document.uri.path = '/workspace/../other/main.py';
    if (change === 'closed') f.document.isClosed = true;
    assert.throws(() => captureActiveEditor(f.editor, options), BridgeRejected);
    assert.equal(f.reads(), 0);
  }
});

test('64KiBをUTF-8 byte数で制限し、複数byte文字・不正Unicode・空コードを拒否する', () => {
  const boundary = '#' + 'あ'.repeat((MAX_CODE_BYTES - 1) / 3);
  assert.equal(new TextEncoder().encode(documentFixture(boundary).capture().code).length, MAX_CODE_BYTES);
  for (const code of [boundary + 'あ', '\ud800', ' \r\n', 'secret\0']) {
    assert.throws(() => documentFixture(code).capture(), BridgeRejected);
  }
});

test('9言語の本文取得を同じ経路で扱い、未知のlanguageIdを拒否する', () => {
  for (const language of ['python', 'c', 'cpp', 'java', 'javascript', 'typescript', 'csharp', 'go', 'rust']) {
    const f = documentFixture(); f.document.languageId = language;
    assert.equal(f.capture().language, language);
  }
  for (const language of ['shellscript', 'unknown', 'C++', 'plaintext']) {
    const f = documentFixture(); f.document.languageId = language;
    assert.throws(f.capture, BridgeRejected);
    assert.equal(f.reads(), 0);
  }
});

test('取得中に版・言語・対象documentが変わったら本文を採用しない', () => {
  for (const change of ['version', 'language', 'document']) {
    const f = documentFixture();
    f.document.getText = () => {
      if (change === 'version') f.document.version++;
      if (change === 'language') f.document.languageId = 'java';
      if (change === 'document') f.editor.document = {};
      return 'SYNTHETIC_SOURCE';
    };
    assert.throws(f.capture, BridgeRejected);
  }
});

test('nonceと固定Origin/source/Binding/世代が揃うモック返信だけを一回受理する', () => {
  const f = bridgeFixture();
  assert.equal(f.bridge.accept(f.event, f.current).language, 'python');
  assert.throws(() => f.bridge.accept(f.event, f.current), BridgeRejected);
  assert.equal(f.bridge.connectionEnabled, false);
  assert.throws(() => f.bridge.request({ explicitAction: true, ...f.current }), BridgeRejected); // nonce衝突も拒否。
});

test('他Project・他User・他Team・旧世代を受理しない', () => {
  for (const field of ['teamId', 'projectId', 'userId', 'generation']) {
    const f = bridgeFixture();
    f.event.data = structuredClone(f.event.data);
    if (field === 'generation') f.event.data.generation++;
    else f.event.data.owner[field]++;
    assert.throws(() => f.bridge.accept(f.event, f.current), BridgeRejected);
  }
  const f = bridgeFixture();
  assert.throws(() => f.bridge.accept(f.event, { owner: { ...f.owner, projectId: 99 }, generation: 7 }), BridgeRejected);
});

test('別タブ・不正Origin・別nonce・ワイルドカードを拒否する', () => {
  for (const change of ['source', 'origin', 'nonce']) {
    const f = bridgeFixture();
    if (change === 'source') f.event.source = {};
    if (change === 'origin') f.event.origin = 'https://evil.example.test';
    if (change === 'nonce') f.event.data.nonce = 'other';
    assert.throws(() => f.bridge.accept(f.event, f.current), BridgeRejected);
  }
  assert.throws(() => new MockBridge({ labMode: true, owner: { teamId: 1, projectId: 2, userId: 3 },
    source: {}, origin: '*', generation: 7 }), BridgeRejected);
});

test('期限切れ・時計逆行・ログアウト・Project切替後は拒否する', () => {
  for (const delta of [5000, -1, NaN]) {
    const f = bridgeFixture(); f.advance(delta);
    assert.throws(() => f.bridge.accept(f.event, f.current), BridgeRejected);
  }
  const f = bridgeFixture(); f.bridge.invalidate();
  assert.throws(() => f.bridge.accept(f.event, f.current), BridgeRejected);
  assert.throws(() => f.bridge.request({ explicitAction: true, ...f.current }), BridgeRejected);
});

test('本文・tokenの余分fieldや生例外を固定エラーへ置換し、秘密を反射しない', () => {
  const f = bridgeFixture(); f.event.data.token = 'SYNTHETIC_TOKEN';
  assert.throws(() => f.bridge.accept(f.event, f.current), error => !String(error).includes('SYNTHETIC_TOKEN'));
  const document = documentFixture(); document.document.getText = () => { throw new Error('SYNTHETIC_SOURCE_TOKEN'); };
  assert.throws(document.capture, error => error instanceof BridgeRejected && !String(error).includes('SYNTHETIC_SOURCE_TOKEN'));
});

test('既定無効で、明示検証モードでも接続・保存・実行の関数を持たない', () => {
  const disabled = new MockBridge();
  assert.equal(disabled.connectionEnabled, false);
  assert.throws(() => disabled.request(), BridgeRejected);
  assert.equal(typeof disabled.connect, 'undefined');
  assert.equal(typeof disabled.execute, 'undefined');
});

test('実ホスト・認証情報付きURL・opaque Origin・非HTTPSを検証経路へ入れない', () => {
  for (const origin of ['https://example.com', 'http://editor.example.test', 'null',
    'https://secret@editor.example.test', 'https://editor.example.test/path']) {
    assert.throws(() => new MockBridge({ labMode: true, owner: { teamId: 1, projectId: 2, userId: 3 },
      source: {}, origin, generation: 7 }), BridgeRejected);
  }
});

test('無制限にnonce履歴を増やさず、64要求の後は新しい接続文脈が必要になる', () => {
  const owner = { teamId: 1, projectId: 2, userId: 3 }, source = {};
  let serial = 0;
  const bridge = new MockBridge({ labMode: true, owner, source, origin: 'https://editor.example.test',
    generation: 1, clock: () => 0, nonce: () => `11111111-1111-4111-8111-${String(++serial).padStart(12, '0')}` });
  for (let index = 0; index < 64; index++) {
    const request = bridge.request({ explicitAction: true, owner, generation: 1 });
    bridge.accept({ origin: 'https://editor.example.test', source, data: { type: 'SNAPSHOT',
      nonce: request.nonce, owner, generation: 1, snapshot: documentFixture().capture() } }, { owner, generation: 1 });
  }
  assert.throws(() => bridge.request({ explicitAction: true, owner, generation: 1 }), BridgeRejected);
});

test('本文内の余分な認証fieldと受信側の世代変更も拒否する', () => {
  const f = bridgeFixture(); f.event.data.snapshot = { ...f.event.data.snapshot, token: 'SYNTHETIC_SECRET' };
  assert.throws(() => f.bridge.accept(f.event, f.current), BridgeRejected);
  const changed = bridgeFixture();
  assert.throws(() => changed.bridge.accept(changed.event, { owner: changed.owner, generation: 8 }), BridgeRejected);
});
