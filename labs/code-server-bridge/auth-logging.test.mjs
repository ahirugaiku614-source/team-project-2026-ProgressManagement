/** 認証情報のログ除去を検査する。実Firebase・ブラウザーを使わず、学生コードも扱わない。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import compiler from '../../frontend/node_modules/@vue/compiler-sfc/dist/compiler-sfc.cjs.js';

const sourceRoot = new URL('../../frontend/src/', import.meta.url);

/**
 * リポジトリ内の信頼済みLogin.vueだけをコンパイル解析し、importをテスト依存へ置き換える。
 * vmで評価するのはこのUI定義だけで、エディター本文や学生のプログラムではない。
 */
function loginFixture({ failure = null } = {}) {
  const filename = fileURLToPath(new URL('components/Login.vue', sourceRoot));
  const source = readFileSync(filename, 'utf8');
  const { descriptor, errors } = compiler.parse(source, { filename });
  assert.deepEqual(errors, []);
  const compiled = compiler.compileScript(descriptor, { id: 'auth-log-test' });
  const script = descriptor.scriptSetup.content;
  const code = compiled.scriptSetupAst.filter(node => node.type !== 'ImportDeclaration')
    .map(node => script.slice(node.start, node.end)).join('\n');
  const logs = [], calls = [], routes = [];
  const response = { user: { async getIdToken() { calls.push('token'); if (failure?.phase === 'token') throw failure.error; return 'SYNTHETIC_TOKEN'; } } };
  const authenticate = method => async (...args) => {
    calls.push([method, ...args]);
    if (failure?.phase === 'auth') throw failure.error;
    return response;
  };
  const context = {
    ref: value => ({ value }), auth: { synthetic: true },
    useRouter: () => ({ replace: async route => { routes.push(route); } }),
    createUserWithEmailAndPassword: authenticate('signup'),
    signInWithEmailAndPassword: authenticate('signin'),
    console: Object.fromEntries(['log', 'error', 'warn', 'info', 'debug'].map(name => [name, (...values) => logs.push(values)])),
  };
  vm.runInNewContext(code + '\nglobalThis.actions = { handleSubmit, email, password, isSignUp, isLoading, errorMessage };', context, { timeout: 1000 });
  context.actions.email.value = 'synthetic@example.test';
  context.actions.password.value = 'SYNTHETIC_PASSWORD';
  return { ...context.actions, logs, calls, routes };
}

test('ログイン・新規登録の認証とtoken取得を維持し、資格情報をconsoleへ出さない', async () => {
  for (const signup of [false, true]) {
    const f = loginFixture(); f.isSignUp.value = signup;
    await f.handleSubmit();
    assert.equal(f.calls[0][0], signup ? 'signup' : 'signin');
    assert.equal(f.calls[0][2], 'synthetic@example.test');
    assert.equal(f.calls[0][3], 'SYNTHETIC_PASSWORD');
    assert.equal(f.calls[1], 'token');
    assert.deepEqual(f.routes, ['/']);
    assert.deepEqual(f.logs, []);
    assert.equal(f.isLoading.value, false);
  }
});

test('認証・token取得失敗時も生エラーや資格情報を出さず、固定メッセージを維持する', async () => {
  for (const phase of ['auth', 'token']) {
    const error = Object.assign(new Error('SYNTHETIC_TOKEN_PASSWORD'), { code: 'auth/invalid-credential' });
    const f = loginFixture({ failure: { phase, error } });
    await f.handleSubmit();
    assert.deepEqual(f.logs, []);
    assert.deepEqual(f.routes, []);
    assert.equal(f.errorMessage.value, 'メールアドレスまたはパスワードが間違っています。');
    assert.equal(f.isLoading.value, false);
  }
});

test('フロントエンドのconsole出力・独自token保存を静的に点検する', () => {
  const walk = directory => readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = new URL(entry.name + (entry.isDirectory() ? '/' : ''), directory);
    return entry.isDirectory() ? walk(path) : /\.(vue|js)$/.test(entry.name) ? [path] : [];
  });
  for (const path of walk(sourceRoot)) {
    const source = readFileSync(path, 'utf8');
    assert.doesNotMatch(source, /\bconsole\s*(?:\.|\[)/, fileURLToPath(path));
    assert.doesNotMatch(source, /\b(?:localStorage|sessionStorage)\s*(?:\.|\[)/, fileURLToPath(path));
  }
});

test('未知の認証エラーやnullでもraw値を表示せず、固定の認証エラーへ変換する', async () => {
  for (const error of [null, new Error('SYNTHETIC_TOKEN'), 'SYNTHETIC_PASSWORD']) {
    const f = loginFixture({ failure: { phase: 'auth', error } });
    await f.handleSubmit();
    assert.equal(f.errorMessage.value, '認証エラーが発生しました。');
    assert.deepEqual(f.logs, []);
    assert.deepEqual(f.routes, []);
    assert.equal(f.isLoading.value, false);
  }
});
