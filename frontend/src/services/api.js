import { auth } from '../firebase'

// 環境変数が未設定の場合はViteのプロキシを利用する
export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || ''
).trim().replace(/\/+$/, '')

// 共通API通信処理
export async function apiRequest(path, options = {}, user = auth.currentUser) {
  if (!user || auth.currentUser?.uid !== user.uid) {
    throw new Error('ログイン情報を確認できません。')
  }

  const token = await user.getIdToken()

  if (auth.currentUser?.uid !== user.uid) {
    throw new Error('ログイン状態が変更されました。')
  }

  const headers = new Headers(options.headers)

  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  headers.set('Authorization', `Bearer ${token}`)

  let response

  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers
    })
  } catch {
    throw new Error(
      'サーバーに接続できません。通信状態を確認してください。'
    )
  }

  if (auth.currentUser?.uid !== user.uid) {
    throw new Error('ログイン状態が変更されました。')
  }

  if (!response.ok) {
    let message = `サーバーとの通信に失敗しました。(${response.status})`

    try {
      const data = await response.json()

      if (typeof data.detail === 'string') {
        message = data.detail
      } else if (Array.isArray(data.detail)) {
        message = '入力内容を確認してください。'
      }
    } catch {
      // JSON形式ではないエラーの場合
    }

    const error = new Error(message)
    error.status = response.status
    throw error
  }

  if (response.status === 204) {
    return null
  }

  try {
    const data = await response.json()

    if (auth.currentUser?.uid !== user.uid) {
      throw new Error('ログイン状態が変更されました。')
    }

    return data
  } catch (error) {
    if (auth.currentUser?.uid !== user.uid) {
      throw new Error('ログイン状態が変更されました。')
    }

    throw new Error('サーバーからの応答データが不正です。')
  }
}