import test from 'node:test'
import assert from 'node:assert/strict'
import {syntheticApi} from '../src/demo/synthetic-api.mjs'
import {createWorkflow, initialState} from '../src/code-sharing/workflow.mjs'

test('明示した合成APIと製品画面モデルで2人の準備・提出・承認・反映・最新版取得を接続する',async()=>{
  const api=syntheticApi(), state=initialState(), data=new Map()
  const storage={getItem:key=>data.get(key)||null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)}
  const model=createWorkflow({request:api.request,state,storage,confirm:()=>true})
  async function select(actor){api.selectActor(actor);await model.bootstrap();await model.selectTeam(1);await model.selectProject(2);await model.selectTask(actor===1?3:4)}
  await select(1);await model.prepareEnvironment();assert.equal(state.basis.state,'ACTIVE')
  api.draft('print("changed")\n');api.save()
  assert.equal(state.result,null) // 保存だけで収集・提出されない。
  await model.connectSession();await model.collect();state.title='合成の変更';await model.preview()
  assert.equal(state.preview.changes.length,1);await model.submit();assert.equal(state.result.change_request_id,1)
  await select(2);await model.prepareEnvironment();const old=state.basis.id
  await model.loadReviews();await model.openReview(1);await model.reviewAction('approve')
  assert.equal(state.detail.review_decision,'APPROVED');await model.reviewAction('integrate')
  assert.equal(state.detail.integration.status,'COMPLETED');assert.equal(api.shared(),'print("changed")\n')
  await model.connectSession();await model.prepareEnvironment(true)
  assert.notEqual(state.basis.id,old);assert.equal(api.current().saved,api.shared());model.dispose()
})

test('合成デモでもdirtyの収集と保存済み未提出変更の更新を拒否する',async()=>{
  const api=syntheticApi()
  await api.request('/api/work/environment',{method:'POST',body:{request_id:'first'}})
  api.draft('changed')
  await assert.rejects(api.request('/api/work/collections',{method:'POST'}),e=>e.code==='UNSAVED_CHANGES')
  api.save()
  await assert.rejects(api.request('/api/work/environment',{method:'POST',body:{request_id:'next',previous_basis_id:'old'}}),e=>e.code==='UNSUBMITTED_CHANGES')
})

test('合成デモは通常APIの失敗時fallbackではなく、未登録結果もUNKNOWNにする',async()=>{
  const api=syntheticApi();assert.deepEqual(await api.request('/api/work/requests/missing'),{outcome:'UNKNOWN',state:'UNKNOWN'})
  await assert.rejects(api.request('/unconfigured'),e=>e.code==='REVIEW_SERVICE_UNAVAILABLE')
})
