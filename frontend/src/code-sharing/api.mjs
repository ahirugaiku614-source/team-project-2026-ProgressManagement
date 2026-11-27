// 認証方法とAPIの起点は既存Editorと同じにする。通信失敗時の代替データや自動再送は持たない。
export class ApiError extends Error {
  constructor(status, code) { super('API操作を完了できませんでした'); this.status = status; this.code = code }
}

export const explanations = {
  ENVIRONMENT_REQUEST_REQUIRED: '「作業環境を準備」から操作してください。準備操作IDを記録してから開始します。',
  ENVIRONMENT_OUTCOME_UNKNOWN: '前の作業環境準備が未確定です。別IDで再実行せず、準備結果を確認してください。',
  EDITOR_GATEWAY_UNAVAILABLE: '安全なcode-server接続経路は未接続です。管理者の配置・実機検証が必要です。',
  CONFIRMATION_NOT_FOUND: 'この変更確認は利用できません。対象を選び直して確認してください。',
  UNSUBMITTED_CHANGES: '保存済みの未提出変更があるため更新しません。先に変更内容を確認してください。',
  ENVIRONMENT_REQUEST_CONFLICT: '同じ準備操作IDが別の操作に使用されています。結果を確認してください。',
  AUTHENTICATION_REQUIRED: 'ログインし直してください。',
  REVIEW_FORBIDDEN: '担当・チーム所属・操作権限を確認してください。',
  REVIEW_TARGET_NOT_FOUND: '選択したプロジェクトとタスクの対応を確認してください。',
  WORK_BASIS_NOT_FOUND: 'この作業基準は利用できません。',
  WORK_BASIS_INACTIVE: '作業基準が失効または終了しています。新しい作業の準備について管理者に確認してください。',
  WORK_BASIS_REVOKED: '作業環境の世代が変わりました。旧作業基準は利用できません。',
  TASK_FINISHED: '完了したタスクでは新しい変更を提出できません。',
  EDIT_SESSION_UNVERIFIED: '編集セッションの本人・作業環境・期限を確認できません。接続条件を確認してください。',
  CAPTURE_POLICY_MISMATCH: 'このセッションの確認方式が異なります。新しい編集セッションを確認してください。',
  UNSAVED_CHANGES: '未保存の変更があります。作業環境で保存してから確認してください。',
  PREVIEW_EXPIRED: '変更確認の有効期限が切れました。もう一度変更を確認してください。',
  PREVIEW_CHANGED: '確認内容と提出内容が異なります。もう一度変更を確認してください。',
  EMPTY_CAPTURE_UNSUPPORTED: '対象ファイルがありません。初期版では全ファイル削除による変更提出はできません。',
  UNSUPPORTED_EMPTY_TREE: 'この空フォルダー構成は初期版で扱えません。管理者に確認してください。',
  COLLECTION_REVIEW_REQUIRED: '収集の結果を確定できません。再収集せず管理者に確認してください。',
  REVIEW_EVIDENCE_CONFLICT: '保存された証拠が一致しません。別のIDで再提出せず、結果を確認してください。',
  COLLECTION_BASIS_CONFLICT: '収集結果は別の作業基準に結び付いています。転用できません。',
  COLLECTION_NOT_FOUND: 'この収集結果は利用できません。',
  REVIEW_LIMIT_EXCEEDED: 'ファイル数やサイズが上限を超えています。',
  INVALID_REQUEST: '入力内容を確認してください。',
  REVIEW_REQUEST_REJECTED: '操作が拒否されました。対象と現在の状態を確認してください。',
  REVIEW_SERVICE_UNAVAILABLE: '変更管理の検証基盤が未接続、または利用できません。',
  INTEGRATION_CONFLICT: '共有コードの更新、または保存状態との不一致により反映できません。結果を確認してください。自動統合は行いません。',
  NETWORK_UNKNOWN: '通信結果が不明です。自動で再提出せず、結果を確認してください。',
  RESPONSE_INVALID: '応答を確認できません。結果を確認してください。',
  OPERATION_RECORD_UNAVAILABLE: '操作IDを安全に保持できません。操作は開始せず、端末の設定を確認してください。',
}

export function explain(error) {
  // サーバーの未知の文言・内部例外・絶対パスをそのまま画面やログへ出さない。
  return explanations[error?.code] || '情報を取得できません。対象と接続状態を確認してください。'
}

export function createApi({ base = '', getToken, fetchImpl = globalThis.fetch, timeout = 15000 }) {
  return async (path, { method = 'GET', body, signal } = {}) => {
    if (!path.startsWith('/api/') || path.includes('..')) throw new ApiError(422, 'INVALID_REQUEST')
    const timer = AbortSignal.timeout(timeout)
    const deadline = signal ? AbortSignal.any([signal, timer]) : timer
    try {
      // SDKからのトークン取得も待ち続けない。SDK内部処理は取消せないが、期限後にAPI送信しない。
      const token = await new Promise((resolve, reject) => {
        const abort = () => reject(new ApiError(0, 'NETWORK_UNKNOWN'))
        if (deadline.aborted) { abort(); return }
        deadline.addEventListener('abort', abort, { once: true })
        Promise.resolve().then(getToken).then(value => {
          deadline.removeEventListener('abort', abort); resolve(value)
        }, () => { deadline.removeEventListener('abort', abort); reject(new ApiError(0, 'NETWORK_UNKNOWN')) })
      })
      if (!token) throw new ApiError(401, 'AUTHENTICATION_REQUIRED')
      const response = await fetchImpl(base + path, {
        method, signal: deadline, cache: 'no-store',
        headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      // 既存差分の上限に余裕を持たせつつ、無制限の応答を保持しない。
      let text = ''
      if (response.body?.getReader) {
        const reader = response.body.getReader(); const decoder = new TextDecoder('utf-8', { fatal: true }); let size = 0
        while (true) {
          const { value, done } = await reader.read(); if (done) break
          size += value.byteLength
          if (size > 2 * 1024 * 1024) { await reader.cancel(); throw new ApiError(502, 'RESPONSE_INVALID') }
          text += decoder.decode(value, { stream: true })
        }
        text += decoder.decode()
      } else {
        text = await response.text()
        if (text.length > 2 * 1024 * 1024) throw new ApiError(502, 'RESPONSE_INVALID')
      }
      let data
      try { data = JSON.parse(text) } catch { throw new ApiError(502, 'RESPONSE_INVALID') }
      if (!response.ok) {
        const legacy = response.status === 401 ? 'AUTHENTICATION_REQUIRED' : response.status === 403 ? 'REVIEW_FORBIDDEN'
          : response.status === 404 ? 'REVIEW_TARGET_NOT_FOUND' : response.status === 409 && path.endsWith('/integrate') ? 'INTEGRATION_CONFLICT'
            : response.status === 409 ? 'REVIEW_EVIDENCE_CONFLICT' : 'REVIEW_SERVICE_UNAVAILABLE'
        const code = typeof data?.detail?.code === 'string' && explanations[data.detail.code] ? data.detail.code : legacy
        throw new ApiError(response.status, code)
      }
      return data
    } catch (error) {
      if (error instanceof ApiError) throw error
      throw new ApiError(0, 'NETWORK_UNKNOWN')
    }
  }
}
