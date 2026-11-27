/** 画面間通信の受理規則だけを模擬する。windowへの接続や実API要求は実装しない。 */
import { BridgeRejected, validateSnapshot } from './capture.mjs';

/** Bindingを形式検査する。認証はしないため、値は将来の認証済み接続層から得る必要がある。 */
function binding(value) {
  if (!value || Object.keys(value).length !== 3 ||
      !['teamId', 'projectId', 'userId'].every(k => Number.isSafeInteger(value[k]) && value[k] > 0)) throw new BridgeRejected();
  return Object.freeze({ teamId: value.teamId, projectId: value.projectId, userId: value.userId });
}

/** mock用HTTPS Originだけを許可する。実ホスト・URL・資格情報・ワイルドカードは受け付けない。 */
function labOrigin(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || !url.hostname.endsWith('.test') || url.origin !== value) throw new BridgeRejected();
    return value;
  } catch { throw new BridgeRejected(); }
}

/** Bindingは全要素一致で比較し、Projectだけ合っている別User/Teamを拒否する。 */
function equalBinding(a, b) {
  return a.teamId === b.teamId && a.projectId === b.projectId && a.userId === b.userId;
}

/**
 * モックイベントを受け取る有限状態機械。既定は無効、labMode=trueでも実通信はできない。
 * Origin/sourceはテスト注入値であり、ブラウザで認証済みという証拠にはならない。
 */
export class MockBridge {
  #owner; #source; #origin; #generation; #clock; #nonce; #pending = null; #closed; #used = new Set();

  /** メモリ内の接続文脈だけを作る。token、URLへの秘密値、永続ストレージは持たない。 */
  constructor({ labMode = false, owner, source, origin, generation, clock = () => performance.now(),
    nonce = () => crypto.randomUUID() } = {}) {
    this.#closed = labMode !== true;
    if (this.#closed) return;
    this.#owner = binding(owner);
    this.#origin = labOrigin(origin);
    if (!source || typeof source !== 'object' || !Number.isSafeInteger(generation) || generation < 1 ||
        typeof clock !== 'function' || typeof nonce !== 'function') throw new BridgeRejected();
    this.#source = source; this.#generation = generation; this.#clock = clock; this.#nonce = nonce;
  }

  /** 検証モードでも常にfalse。実エディター、Vue、実行APIへ接続する機能は存在しない。 */
  get connectionEnabled() { return false; }

  /** 明示操作に一度だけ対応する要求値を返す。送信はせず、他の保留要求も上書きしない。 */
  request({ explicitAction, owner, generation } = {}) {
    try {
      if (this.#closed || explicitAction !== true || this.#pending ||
          !equalBinding(this.#owner, binding(owner)) || generation !== this.#generation) throw new BridgeRejected();
      const nonce = this.#nonce(), now = this.#clock();
      if (typeof nonce !== 'string' || !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(nonce) ||
          this.#used.has(nonce) || this.#used.size >= 64 || !Number.isFinite(now)) throw new BridgeRejected();
      this.#used.add(nonce); // 衝突時も再発行せず拒否。上限後は新しい接続文脈の明示確立が必要。
      this.#pending = { nonce, issued: now };
      return Object.freeze({ type: 'CAPTURE', nonce, owner: this.#owner, generation: this.#generation });
    } catch { throw new BridgeRejected(); }
  }

  /**
   * 期待するwindowとOriginからの一回の返信だけを検査する。返値は本文のモックsnapshotのみ。
   * 現在Binding/世代は受信時の呼出し元で再確認する前提。自己申告値で認可を実装しない。
   */
  accept(event, { owner, generation } = {}) {
    try {
      if (this.#closed || !this.#pending || event?.origin !== this.#origin || event.source !== this.#source) throw new BridgeRejected();
      const data = event.data, pending = this.#pending;
      if (!data || data.nonce !== pending.nonce) throw new BridgeRejected();
      this.#pending = null; // 同じnonceの不正返信も再試行せず、新しい明示操作を要求する。
      const expected = ['type', 'nonce', 'owner', 'generation', 'snapshot'];
      const age = this.#clock() - pending.issued;
      if (Object.keys(data).length !== expected.length || expected.some(k => !Object.hasOwn(data, k)) ||
          data.type !== 'SNAPSHOT' || !Number.isFinite(age) || age < 0 || age >= 5000 ||
          generation !== this.#generation || data.generation !== this.#generation ||
          !equalBinding(binding(owner), this.#owner) || !equalBinding(binding(data.owner), this.#owner)) throw new BridgeRejected();
      return validateSnapshot(data.snapshot);
    } catch { throw new BridgeRejected(); }
  }

  /** ログアウト・Project切替・frame再読込時の失効を模擬し、保留nonceも永久に破棄する。 */
  invalidate() { this.#closed = true; this.#pending = null; }
}
