import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createWorkflow, initialState, permissions, reviewLabel, serverTime } from '../src/code-sharing/workflow.mjs'
import { createApi, ApiError, explain } from '../src/code-sharing/api.mjs'

// 期待値は固定した合成ID・応答から定義する。実Firebase・実API・実ファイル収集には接続しない。
const BID = '11111111-1111-4111-8111-111111111111', SID = '22222222-2222-4222-8222-222222222222'
const CID = '33333333-3333-4333-8333-333333333333', REQUEST = '44444444-4444-4444-8444-444444444444'
const D = '55555555-5555-4555-8555-555555555555', SESSION = '66666666-6666-4666-8666-666666666666'
const row = { id: 9, project_id: 2, title: '表示を改善', submitted_by: 2, submitter_name: '開発メンバーB',
  state: 'OPEN', review_decision: 'PENDING', reviewer_id: null, base_commit: 'a'.repeat(40), snapshot_commit: 'b'.repeat(40) }
const diff = { base_commit: row.base_commit, snapshot_commit: row.snapshot_commit, files: [{ path: 'src/main.txt', change: 'M', patch: '+合成テキスト', binary: false, truncated: false }] }
const preview = { confirmation_id: CID, expires_at: '2099-01-01T00:00:00', changes: [{ path: 'src/main.txt', change: 'MODIFIED' }], no_change: false, main_differs: false, excluded_paths: [], excluded_policy_version: 1 }
function memory() { const m = new Map(); return { getItem: k => m.get(k) || null, setItem: (k,v) => m.set(k,v), removeItem: k => m.delete(k), entries: m } }
function fixture({ override, session = true, storage = memory(), clock = () => Date.parse('2026-01-01T00:00:00Z') } = {}) {
  const s = initialState(), calls = []
  Object.assign(s, { me: { id: 1 }, teams: [{ id: 1, name: 'チームA' }], teamId: 1, projectId: 2, taskId: 3, membership: true,
    projects: [{ id: 2, name: 'Project A' }, { id: 8, name: 'Project B' }], tasks: [{ id: 3, title: '表示改善', assignee_id: 1 }],
    basis: { id: BID, state: 'ACTIVE' }, journal: { basis_id: BID, request_id: REQUEST, submission_id: SID, pending: null },
    result: { collection_state: 'SUBMITTED', conversion_state: 'NOT_REQUESTED' }, title: '表示を改善' })
  const request = async (path, options = {}) => {
    const c = { path, method: options.method || 'GET', body: options.body }; calls.push(c)
    const special = override?.(c)
    if (special !== undefined) return special
    if (path.endsWith('/preview')) return structuredClone(preview)
    if (path.endsWith('/change-request')) return { conversion_state: 'READY', change_request_id: 9, collection_state: 'SUBMITTED' }
    if (path.endsWith('/deletion-candidates')) return { candidates: [{ id: D, path: 'src/old.txt' }] }
    if (path.endsWith('/deletion-intents')) return { deletion_id: D, path: 'src/old.txt' }
    if (path.endsWith('/submission-permits')) return { permit: '合成許可値' }
    if (path.endsWith('/collections')) return { submission_id: SID, collection_state: 'SUBMITTED' }
    if (path.includes('/collections/')) return { conversion_state: 'READY', change_request_id: 9, collection_state: 'SUBMITTED' }
    if (path.endsWith('/work-bases/current')) return { basis: { id: BID, state: 'ACTIVE' } }
    if (path.endsWith('/work-bases')) return { id: BID, state: 'ACTIVE' }
    if (path.endsWith('/editor')) return { state: 'UNAVAILABLE', launch_enabled: false }
    if (path.endsWith('/integration')) return null
    if (path.endsWith('/diff')) return structuredClone(diff)
    if (/\/change-requests\/\d+$/.test(path)) return structuredClone(row)
    if (path.includes('/change-requests?')) return [structuredClone(row)]
    if (path === '/api/me') return { id: 1 }
    if (path === '/api/teams') return [{ id: 1, name: 'チームA' }]
    if (path.endsWith('/projects')) return [{ id: 2, name: 'Project A' }]
    if (path.endsWith('/tasks')) return [{ id: 3, assignee_id: 1 }, { id: 4, assignee_id: 2 }]
    if (path.endsWith('/members')) return [{ user_id: 1, role: 'member' }]
    throw new Error('未定義の合成経路')
  }
  const m = createWorkflow({ request, state: s, storage, now: clock, makeId: () => REQUEST,
    confirm: () => true, ...(session ? { sessionSource: async () => SESSION } : {}) })
  return { s, m, calls, storage }
}
const posts = f => f.calls.filter(c => c.method === 'POST')
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

test('一覧・詳細・差分を既存APIから読み、画面表示だけで提出しない', async () => {
  const f=fixture(); await f.m.loadReviews(); await f.m.openReview(9)
  assert.equal(f.s.reviews.length,1);assert.equal(f.s.detail.id,9);assert.equal(f.s.diff.files.length,1);assert.equal(posts(f).length,0)
})
test('起動・再読込みでPOSTせず、現在ユーザーの担当だけを選ぶ',async()=>{
  const f=fixture();await f.m.bootstrap();await f.m.selectTeam(1)
  assert.deepEqual(f.s.tasks.map(t=>t.id),[3]);assert.equal(posts(f).length,0)
})
test('認証ユーザーが変わると古いコンテキストを消す',async()=>{
  const f=fixture();await f.m.bootstrap();assert.equal(f.s.projectId,null);assert.equal(f.s.journal,null)
})
test('Team切替で古い通信応答を破棄する',async()=>{
  const d=deferred(), f=fixture({override:c=>c.path==='/api/teams/1/projects'?d.promise:undefined})
  const old=f.m.selectTeam(1);await f.m.selectTeam(null);d.resolve([{id:2}]);await old
  assert.equal(f.s.teamId,null);assert.equal(f.s.projects.length,0)
})
test('Project切替で旧一覧応答・確認IDを持ち越さない',async()=>{
  const d=deferred(),f=fixture({override:c=>c.path.includes('/projects/2/change-requests?')?d.promise:c.path.includes('/projects/8/change-requests?')?[{...row,id:10,project_id:8}]:undefined})
  f.s.preview={...preview};const old=f.m.loadReviews();await f.m.selectProject(8);d.resolve([row]);await old
  assert.equal(f.s.projectId,8);assert.equal(f.s.preview,null);assert.equal(f.s.reviews[0].project_id,8)
})
test('Task切替で収集結果と入力をクリアする',async()=>{
  const f=fixture();f.s.preview={...preview};await f.m.selectTask(null)
  assert.equal(f.s.journal,null);assert.equal(f.s.title,'');assert.equal(f.s.preview,null)
})
test('詳細の応答順が逆でも新しい選択を守る',async()=>{
  const d=deferred(),f=fixture({override:c=>c.path.endsWith('/change-requests/9')?d.promise:c.path.endsWith('/change-requests/10')?{...row,id:10}:undefined})
  const old=f.m.openReview(9);await f.m.openReview(10);d.resolve(row);await old;assert.equal(f.s.detail.id,10)
})
test('別Projectの詳細応答を採用しない',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/change-requests/9')?{...row,project_id:8}:undefined});await f.m.openReview(9)
  assert.equal(f.s.detail,null);assert.ok(f.s.detailError)
})
test('確認だけではChangeRequestを登録しない',async()=>{
  const f=fixture();await f.m.preview();assert.equal(f.s.preview.confirmation_id,CID)
  assert.equal(posts(f).length,1);assert.ok(posts(f)[0].path.endsWith('/preview'))
})
test('明示提出のときだけ固定確認IDで登録する',async()=>{
  const f=fixture();await f.m.preview();await f.m.submit()
  const sent=posts(f).filter(c=>c.path.endsWith('/change-request'));assert.equal(sent.length,1);assert.equal(sent[0].body.confirmation_id,CID)
  assert.equal(f.s.result.change_request_id,9);assert.ok(f.s.message.includes('レビュー依頼'))
})
test('タイトル変更後は提出できず再確認を求める',async()=>{
  const f=fixture();await f.m.preview();f.s.title='別の変更';await f.m.submit()
  assert.equal(posts(f).filter(c=>c.path.endsWith('/change-request')).length,0);assert.equal(f.s.preview,null)
})
test('期限切れは提出しない・UTCを現地時刻と混同しない',async()=>{
  const f=fixture({clock:()=>Date.parse('2100-01-01T00:00:00Z')});await f.m.preview();await f.m.submit()
  assert.equal(posts(f).length,1);assert.equal(serverTime('2026-01-01T00:00:00'),Date.parse('2026-01-01T00:00:00Z'))
})
test('失効基準から確認・提出しない',async()=>{
  const f=fixture();f.s.basis.state='REVOKED';await f.m.preview();await f.m.submit();assert.equal(posts(f).length,0)
})
test('実セッション未接続なら収集・確認・提出を開始できない',async()=>{
  const f=fixture({session:false});f.s.journal=null;await f.m.collect();await f.m.preview();assert.equal(posts(f).length,0);assert.equal(f.s.sessionConnected,false)
})
test('収集は許可発行と明示操作でのみ行い、収集成功をレビュー成功にしない',async()=>{
  const f=fixture();f.s.journal=null;await f.m.collect()
  assert.equal(posts(f).length,2);assert.equal(f.s.result.collection_state,'SUBMITTED');assert.equal(f.s.result.change_request_id,undefined)
  assert.ok(f.s.message.includes('まだ作成していません'));assert.equal(posts(f)[1].body.request_id,REQUEST)
})
test('二重クリックで二重提出しない',async()=>{
  const d=deferred(),f=fixture({override:c=>c.path.endsWith('/change-request')?d.promise:undefined});await f.m.preview()
  const first=f.m.submit();await f.m.submit();d.resolve({conversion_state:'READY',change_request_id:9});await first
  assert.equal(posts(f).filter(c=>c.path.endsWith('/change-request')).length,1)
})
test('通信失敗後に新IDで自動再提出しない・結果APIで確認する',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/change-request')?Promise.reject(new ApiError(0,'NETWORK_UNKNOWN')):undefined})
  await f.m.preview();await f.m.submit();await f.m.submit()
  assert.equal(posts(f).filter(c=>c.path.endsWith('/change-request')).length,1);assert.equal(f.s.journal.pending,'SUBMIT')
  await f.m.checkResult();assert.equal(f.s.journal.pending,null);assert.equal(f.s.result.change_request_id,9)
})
test('結果不明の操作IDは再読込み後も保持する',async()=>{
  const store=memory();store.setItem('changes:v1:1:1:2:3',JSON.stringify({basis_id:BID,request_id:REQUEST,submission_id:SID,pending:'SUBMIT'}))
  const f=fixture({storage:store});await f.m.refreshWork();assert.equal(f.s.journal.pending,'SUBMIT');assert.equal(f.m.canPreview(),false)
})
test('収集ID未取得の応答不明はrequest IDで照会し再収集しない',async()=>{
  const f=fixture({override:c=>c.path.includes('/requests/')?{outcome:'UNKNOWN'}:c.path.endsWith('/collections')?Promise.reject(new ApiError(0,'NETWORK_UNKNOWN')):undefined});f.s.journal=null
  await f.m.collect();await f.m.collect();await f.m.checkResult()
  assert.equal(posts(f).filter(c=>c.path.endsWith('/collections')).length,1);assert.ok(f.s.error.includes('未実行を意味しません'))
  assert.equal(f.calls.filter(c=>c.path.endsWith('/requests/'+REQUEST)).length,1)
})

test('request IDで収集IDを回復しても再提出しない',async()=>{
  const f=fixture({override:c=>c.path.includes('/requests/')?{outcome:'RECORDED',submission_id:SID}:undefined})
  f.s.journal.submission_id=null;f.s.journal.pending='COLLECTION'
  await f.m.checkResult();assert.equal(f.s.journal.submission_id,SID);assert.equal(posts(f).length,0)
})

test('編集セッションを自己申告で有効にしない',async()=>{
  const f=fixture({session:false,override:c=>c.path.endsWith('/editing-sessions')?Promise.reject(new ApiError(503,'EDIT_SESSION_UNVERIFIED')):undefined})
  await f.m.connectSession();assert.equal(f.s.sessionConnected,false);assert.ok(f.s.error)
})

test('最新版取得は管理側へ明示要求し通信失敗後に自動再要求しない',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/environment')?Promise.reject(new ApiError(0,'NETWORK_UNKNOWN')):undefined})
  await f.m.prepareEnvironment(true);await f.m.prepareEnvironment(true)
  assert.equal(posts(f).filter(c=>c.path.endsWith('/environment')).length,1)
})

test('準備後の再照会中にProjectを変えても旧成功表示を持ち越さない',async()=>{
  const d=deferred(),f=fixture({override:c=>c.path.endsWith('/environment')?{state:'CONFIRMED',basis_id:BID}:c.path.endsWith('/work-bases/current')?d.promise:undefined})
  f.s.basis=null;f.s.journal=null
  const old=f.m.prepareEnvironment();await new Promise(resolve=>setImmediate(resolve))
  await f.m.selectProject(8);d.resolve({basis:{id:BID,state:'ACTIVE'}});await old
  assert.equal(f.s.projectId,8);assert.equal(f.s.message,'');assert.equal(f.s.basis,null)
})

test('タスク関連情報の遅い応答も新しいProjectへ混入しない',async()=>{
  const d=deferred(),f=fixture({override:c=>c.path.endsWith('/task-context')?d.promise:undefined})
  const old=f.m.openReview(9);await new Promise(resolve=>setImmediate(resolve));await f.m.selectProject(8)
  d.resolve({task_id:3});await old;assert.equal(f.s.detail,null);assert.equal(f.s.projectId,8)
})
test('削除候補を取得しても自動承認しない',async()=>{
  const f=fixture();await f.m.loadCandidates();assert.equal(f.s.selectedDeletes.length,0);assert.equal(posts(f).length,0)
})
test('候補以外の削除と未確認削除は送信しない',async()=>{
  const f=fixture();await f.m.loadCandidates();f.s.selectedDeletes=[CID];await f.m.confirmDeletions();assert.equal(posts(f).length,0)
})
test('選択した候補を明示確認して証拠IDを使う',async()=>{
  const f=fixture();await f.m.loadCandidates();f.s.selectedDeletes=[D];await f.m.confirmDeletions();await f.m.preview()
  assert.deepEqual(posts(f).at(-1).body.deletion_ids,[D]);assert.equal(f.s.journal.pending,null)
})
test('削除確認の通信不明では別の確認操作を自動発行しない',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/deletion-intents')?Promise.reject(new ApiError(0,'NETWORK_UNKNOWN')):undefined})
  await f.m.loadCandidates();f.s.selectedDeletes=[D];await f.m.confirmDeletions()
  assert.equal(f.s.journal.pending,'DELETE');assert.equal(f.m.canPreview(),false)
})
test('変更なしを確定してもレビュー作成成功とは表示しない',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/preview')?{...preview,no_change:true,changes:[]}:c.path.endsWith('/change-request')?{conversion_state:'NO_CHANGE',change_request_id:null}:undefined})
  await f.m.preview();await f.m.submit();assert.equal(f.s.result.change_request_id,null);assert.ok(f.s.message.includes('作成していません'))
})
test('期限切れなど確定拒否では再確認を可能にする',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/change-request')?Promise.reject(new ApiError(409,'PREVIEW_EXPIRED')):undefined})
  await f.m.preview();await f.m.submit();assert.equal(f.s.journal.pending,null);assert.equal(f.s.preview,null);assert.equal(f.m.canPreview(),true)
})
test('権限不足・自己承認・条件不明では操作しない',async()=>{
  const f=fixture();await f.m.openReview(9);f.s.membership=false;await f.m.reviewAction('approve');assert.equal(posts(f).length,0)
  f.s.membership=true;f.s.detail.submitted_by=1;assert.equal(permissions(f.s).review,false)
  f.s.detail.submitted_by=2;f.s.diff=null;assert.equal(permissions(f.s).review,false)
})
test('差分失敗で承認せず、画面の状態は保つ',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/diff')?Promise.reject(new ApiError(503,'REVIEW_SERVICE_UNAVAILABLE')):undefined})
  await f.m.openReview(9);assert.equal(f.s.detail.id,9);assert.ok(f.s.detailError);assert.equal(permissions(f.s).review,false)
})
test('承認は共有コードへの反映と区別する',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/approve')?{...row,review_decision:'APPROVED'}:undefined});await f.m.openReview(9);await f.m.reviewAction('approve')
  assert.equal(posts(f).length,1);assert.ok(f.s.message.includes('別の操作'))
})
test('修正理由なしで修正依頼を送らない',async()=>{
  const f=fixture();await f.m.openReview(9);await f.m.reviewAction('request-changes');assert.equal(posts(f).length,0)
})
test('修正依頼は既存APIへ理由だけを送る',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/request-changes')?{...row,review_decision:'CHANGES_REQUESTED'}:undefined});await f.m.openReview(9);f.s.reason='表示を見直してください';await f.m.reviewAction('request-changes')
  assert.deepEqual(posts(f)[0].body,{reason:'表示を見直してください'})
})
test('統合は別メンバーの承認と既知の未反映状態を必要とする',async()=>{
  const f=fixture();await f.m.openReview(9);assert.equal(permissions(f.s).integrate,false)
  Object.assign(f.s.detail,{review_decision:'APPROVED',reviewer_id:3,integration:null});assert.equal(permissions(f.s).integrate,true)
  f.s.detail.integration={status:'PREPARED'};assert.equal(permissions(f.s).integrate,false)
})
test('統合の確認ダイアログをキャンセルした場合はPOSTしない',async()=>{
  const f=fixture(),m=createWorkflow({state:f.s,request:async()=>{throw Error('呼出禁止')},confirm:()=>false})
  f.s.detail={...row,review_decision:'APPROVED',reviewer_id:3,integration:null};f.s.diff=diff;await m.reviewAction('integrate')
})
test('main不一致で自動統合・再送せず固定メッセージを返す',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/integrate')?Promise.reject(new ApiError(409,'INTEGRATION_CONFLICT')):undefined})
  await f.m.openReview(9);Object.assign(f.s.detail,{review_decision:'APPROVED',reviewer_id:3});await f.m.reviewAction('integrate');await f.m.reviewAction('integrate')
  assert.equal(posts(f).length,1);assert.ok(f.s.error.includes('自動統合'));assert.equal(f.s.reviewUncertain,true)
})
test('保存済み状態・レビュー・統合のラベルを混同しない',()=>{
  assert.equal(reviewLabel({...row,integration:{status:'COMPLETED'}}),'統合済み')
  assert.equal(reviewLabel({...row,review_decision:'CHANGES_REQUESTED',integration:null}),'修正が必要')
  assert.equal(reviewLabel({...row,integrationUnknown:true}),'確認が必要')
})
test('保存処理から変更提出を呼び出していない',async()=>{
  const editor=await readFile(new URL('../src/components/Editor.vue',import.meta.url),'utf8')
  const save=editor.slice(editor.indexOf('async function saveFile'),editor.indexOf('async function deleteFile'))
  assert.ok(save.length>0);assert.ok(!save.includes('work-bases'));assert.ok(!save.includes('change-requests'))
})
test('既存ホーム・進捗管理からコード共有への入口を追加する',async()=>{
  for(const file of ['Home.vue','schedule.vue'])assert.ok((await readFile(new URL('../src/components/'+file,import.meta.url),'utf8')).includes("name: 'CodeSharing'"))
})
test('APIクライアントはBearerを使用し、認証不能時は送信しない',async()=>{
  let sent=0
  const api=createApi({getToken:async()=>null,fetchImpl:async()=>{sent++}})
  await assert.rejects(api('/api/teams'),e=>e.code==='AUTHENTICATION_REQUIRED');assert.equal(sent,0)
})
test('APIエラーの未知の内部文言を画面へ出さない',async()=>{
  const api=createApi({getToken:async()=>'合成認証値',fetchImpl:async()=>new Response(JSON.stringify({detail:'private-internal-value'}),{status:503})})
  try{await api('/api/teams');assert.fail()}catch(e){assert.ok(!explain(e).includes('private-internal-value'))}
})
test('APIの不正JSONとサイズ超過は拒否する',async()=>{
  for(const text of ['not-json','x'.repeat(2*1024*1024+1)]){
    const api=createApi({getToken:async()=>'合成認証値',fetchImpl:async()=>new Response(text)})
    await assert.rejects(api('/api/teams'),e=>e.code==='RESPONSE_INVALID')
  }
})
test('提出本文や許可値を端末の操作ID記録へ保存しない',async()=>{
  const f=fixture();f.s.journal=null;await f.m.collect();await f.m.preview()
  const stored=[...f.storage.entries.values()].join('');assert.ok(!stored.includes('合成許可値'));assert.ok(!stored.includes('confirmation_id'));assert.ok(!stored.includes('表示を改善'))
})
test('操作IDを保持できない場合は変更操作を送信しない',async()=>{
  const bad={getItem:()=>null,setItem:()=>{throw Error('保持不能')}}
  const f=fixture({storage:bad});f.s.journal=null;await f.m.collect();assert.equal(posts(f).length,0);assert.ok(f.s.error.includes('保持できません'))
})
test('破損した操作記録から新IDで再開しない',async()=>{
  for(const value of ['broken','false','0','[]','{}']){
    const store=memory();store.setItem('changes:v1:1:1:2:3',value)
    const f=fixture({storage:store});await f.m.refreshWork();await f.m.collect();assert.equal(posts(f).length,0);assert.equal(f.s.journal.pending,'INVALID')
  }
})
test('統合の成功応答と結果照会を既存APIから確認する',async()=>{
  let done=false
  const f=fixture({override:c=>c.path.endsWith('/change-requests/9')?{...row,review_decision:'APPROVED',reviewer_id:3}
    :c.path.endsWith('/integrate')?(done=true,{change_request_id:9,project_id:2,status:'COMPLETED'})
      :c.path.endsWith('/integration')?(done?{change_request_id:9,project_id:2,status:'COMPLETED'}:null):undefined})
  await f.m.openReview(9);await f.m.reviewAction('integrate')
  assert.equal(f.s.detail.integration.status,'COMPLETED');assert.ok(f.s.message.includes('反映を確認'))
  assert.equal(f.calls.filter(c=>['PUT','PATCH','DELETE'].includes(c.method)).length,0)
})
test('不正構造の差分では描画・承認を許可しない',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/diff')?{...diff,files:[null]}:undefined});await f.m.openReview(9)
  assert.equal(f.s.diff,null);assert.ok(f.s.detailError);assert.equal(permissions(f.s).review,false)
})
test('不正な確認応答から確認済みに遷移しない',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/preview')?{...preview,changes:[null]}:undefined});await f.m.preview()
  assert.equal(f.s.preview,null);assert.equal(f.m.canSubmit(),false)
})
test('セッション取得待ちの間にTaskを切り替えても操作を持ち越さない',async()=>{
  const d=deferred(),s=initialState(),calls=[]
  Object.assign(s,{me:{id:1},membership:true,teamId:1,projectId:2,taskId:3,basis:{id:BID,state:'ACTIVE'}})
  const m=createWorkflow({state:s,storage:memory(),sessionSource:()=>d.promise,request:async(...a)=>{calls.push(a)}})
  const pending=m.collect();await m.selectTask(null);d.resolve(SESSION);await pending
  assert.equal(calls.length,0);assert.equal(s.journal,null)
})
test('作業開始の応答が不明なら再開操作を重ねない',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/work-bases')?Promise.reject(new ApiError(0,'NETWORK_UNKNOWN')):undefined})
  f.s.basis=null;f.s.journal=null;await f.m.start();await f.m.start()
  assert.equal(posts(f).length,1);assert.equal(f.s.journal.pending,'START')
})
test('既知の失効・空収集・未保存・接続不能を日本語で説明する',()=>{
  for(const code of ['WORK_BASIS_INACTIVE','EMPTY_CAPTURE_UNSUPPORTED','UNSAVED_CHANGES','EDIT_SESSION_UNVERIFIED'])
    assert.ok(explain({code}).length>10)
})
test('通信タイムアウトを自動再送しない',async()=>{
  let sent=0;const api=createApi({getToken:async()=>'合成認証値',fetchImpl:async()=>{sent++;throw new DOMException('timeout','TimeoutError')}})
  await assert.rejects(api('/api/teams'),e=>e.code==='NETWORK_UNKNOWN');assert.equal(sent,1)
})
test('収集の状態が未取得・処理中・要確認なら変更確認を禁止する',async()=>{
  const f=fixture()
  for(const result of [null,{collection_state:'PROCESSING'},{collection_state:'REVIEW_REQUIRED'},{collection_state:'SUBMITTED',conversion_state:'READY'}]){
    f.s.result=result;await f.m.preview();assert.equal(posts(f).length,0)
  }
})
test('基準の再確認が失敗した場合は古いACTIVEを使わない',async()=>{
  const f=fixture({override:c=>c.path.endsWith('/work-bases/current')?Promise.reject(new ApiError(403,'REVIEW_FORBIDDEN')):undefined})
  await f.m.refreshWork();assert.equal(f.s.basis,null);assert.equal(f.m.canPreview(),false)
})
test('確認の更新に失敗した場合は前の確認IDを残さない',async()=>{
  let failed=false;const f=fixture({override:c=>failed&&c.path.endsWith('/preview')?Promise.reject(new ApiError(503,'REVIEW_SERVICE_UNAVAILABLE')):undefined})
  await f.m.preview();failed=true;await f.m.preview();assert.equal(f.s.preview,null)
})
test('トークン取得が遅れても期限後にAPI送信しない',async()=>{
  let sent=0;const api=createApi({timeout:5,getToken:()=>new Promise(r=>setTimeout(()=>r('合成認証値'),30)),fetchImpl:async()=>{sent++;return new Response('{}')}})
  await assert.rejects(api('/api/teams'),e=>e.code==='NETWORK_UNKNOWN');assert.equal(sent,0)
})
