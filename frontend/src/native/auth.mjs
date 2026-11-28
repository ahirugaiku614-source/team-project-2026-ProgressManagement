// 合成実機入口専用。通常のFirebase入口からimportしない。
// tokenはこのclosureのメモリだけに保持し、URL・storage・ログへ保存しない。
export function syntheticLogin(fetchImpl=globalThis.fetch) {
  let token=null
  async function login(account,password) {
    if (!['A','B'].includes(account) || typeof password!=='string' || password.length<20) throw Error('合成アカウントとパスワードを確認してください。')
    const response=await fetchImpl('/api/synthetic/login',{method:'POST',cache:'no-store',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({account,password}),signal:AbortSignal.timeout(10000)})
    let result
    try { result=await response.json() } catch { throw Error('認証結果を確認できません。自動再送しません。') }
    if (!response.ok || result.environment!=='SYNTHETIC_ONLY' || !/^[A-Za-z0-9_-]{43}$/.test(result.token||'')
      || !Number.isInteger(result.expires_at) || result.expires_at*1000<=Date.now()) throw Error('合成認証を確認できません。資格情報と環境を確認してください。')
    token=result.token
  }
  async function logout() {
    if (!token) return
    const response=await fetchImpl('/api/synthetic/logout',{method:'POST',cache:'no-store',
      headers:{Authorization:`Bearer ${token}`},signal:AbortSignal.timeout(10000)})
    let result
    try { result=await response.json() } catch { throw Error('ログアウト結果が不明です。変更操作を止めてください。') }
    if (!response.ok || result.state!=='REVOKED' || result.environment!=='SYNTHETIC_ONLY') throw Error('ログアウト結果が不明です。再確認するまで変更操作を行わないでください。')
    token=null
  }
  return {login,logout,getToken:async()=>token,connected:()=>!!token}
}
