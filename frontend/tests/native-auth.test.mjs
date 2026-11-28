import test from 'node:test'
import assert from 'node:assert/strict'
import {syntheticLogin} from '../src/native/auth.mjs'

const success=()=>({ok:true,json:async()=>({environment:'SYNTHETIC_ONLY',token:'a'.repeat(43),expires_at:Math.floor(Date.now()/1000)+60})})
test('合成実機認証は明示loginだけを送信する',async()=>{
  const calls=[];const auth=syntheticLogin(async(...args)=>{calls.push(args);return success()})
  assert.equal(await auth.getToken(),null);assert.equal(calls.length,0)
  await auth.login('A','synthetic-password-only-123')
  assert.equal(calls.length,1);assert.equal(calls[0][0],'/api/synthetic/login')
  assert.equal(await auth.getToken(),'a'.repeat(43))
})
test('UIDを合成アカウントとして使用できない',async()=>{
  let calls=0;const auth=syntheticLogin(async()=>{calls++;return success()})
  await assert.rejects(auth.login('uid-other','synthetic-password-only-123'));assert.equal(calls,0)
})
test('通常応答や架空の成功を採用しない',async()=>{
  const auth=syntheticLogin(async()=>({ok:true,json:async()=>({token:'a'.repeat(43),expires_at:9999999999})}))
  await assert.rejects(auth.login('A','synthetic-password-only-123'));assert.equal(auth.connected(),false)
})
test('通信失敗時に自動再送しない',async()=>{
  let calls=0;const auth=syntheticLogin(async()=>{calls++;throw Error('synthetic network failure')})
  await assert.rejects(auth.login('A','synthetic-password-only-123'));assert.equal(calls,1)
})
test('logout成功後はtokenを破棄する',async()=>{
  const auth=syntheticLogin(async path=>path.endsWith('/logout')?{ok:true,json:async()=>({state:'REVOKED',environment:'SYNTHETIC_ONLY'})}:success());await auth.login('B','synthetic-password-only-123')
  await auth.logout();assert.equal(await auth.getToken(),null)
})
test('logout不明を成功と表示しない',async()=>{
  let calls=0;const auth=syntheticLogin(async()=>++calls===1?success():{ok:false})
  await auth.login('A','synthetic-password-only-123');await assert.rejects(auth.logout());assert.equal(calls,2)
})
