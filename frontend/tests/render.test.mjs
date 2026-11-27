import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { createSSRApp, h } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createWorkflow, initialState } from '../src/code-sharing/workflow.mjs'

// 実Firebaseをimportするコンテナーではなく、実際の表示部品を合成propsで描画する。
// listenは呼ばない。HTTP/UDS/外部APIに接続する画面プレビューモードは製品へ追加しない。
const server = await createServer({ configFile: false, plugins: [vue()], logLevel: 'silent',
  server: { middlewareMode: true, hmr: false, watch: null }, appType: 'custom' })
let Panel
try { Panel = (await server.ssrLoadModule('/src/components/CodeSharingPanel.vue')).default }
catch (error) { await server.close(); throw error }
test.after(async () => { await server.close() })
async function render(state) {
  const model = createWorkflow({ state, request: async () => { throw Error('通信禁止') } })
  return renderToString(createSSRApp({ render: () => h(Panel, { state, model }) }))
}
function sample() {
  const state = initialState()
  Object.assign(state, { me: { id: 1 }, teamId: 1, projectId: 2, taskId: 3, membership: true,
    teams: [{ id: 1, name: '合成チームA' }], projects: [{ id: 2, name: '合成プロジェクト' }],
    tasks: [{ id: 3, title: '検索表示を改善', status: 'IN_PROGRESS' }],
    basis: { id: '11111111-1111-4111-8111-111111111111', state: 'ACTIVE', started_at: '2026-01-01T00:00:00' },
    environment: { state: 'UNAVAILABLE', launch_enabled: false },
    detail: { id: 1, project_id: 2, title: '検索結果の表示を改善', submitter_name: '合成メンバーB',
      submitted_by: 2, review_decision: 'PENDING', state: 'OPEN', integration: null, created_at: '2026-01-01T00:00:00' },
    reviews: [{ id: 1, title: '検索結果の表示を改善', submitter_name: '合成メンバーB', review_decision: 'PENDING', integration: null }],
    diff: { files: [{ path: 'src/search.txt', change: 'M', patch: '-以前の表示\n+新しい表示', binary: false }] } })
  return state
}
test('実Vue部品が一覧・詳細・差分を描画する', async () => {
  const html = await render(sample())
  for (const word of ['コード共有とレビュー','自分の作業','チームの変更一覧','レビュー待ち','src/search.txt','新しい表示']) assert.ok(html.includes(word))
})
test('HTML・スクリプト・イベント属性をテキストとして描画する', async () => {
  const s = sample(), attack = '<script>alert(1)</script><img src=x onerror=alert(1)>'
  s.detail.title=attack;s.detail.review_reason=attack;s.diff.files[0].path=attack;s.diff.files[0].patch=attack
  const html=await render(s)
  assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img src=x'));assert.ok(html.includes('&lt;script&gt;'))
  const source=await readFile(new URL('../src/components/CodeSharingPanel.vue',import.meta.url),'utf8')
  assert.ok(!source.includes('v-html'));assert.ok(!source.includes('innerHTML'))
})
test('未接続時に保存済み・取得成功と見せず、収集を無効化する', async () => {
  const html=await render(sample())
  assert.ok(html.includes('保存状態を確認できない'));assert.ok(/disabled[^>]*>保存済み変更を固定して確認/.test(html))
})

test('方式Cの未保存除外と確認時点の固定内容を説明する', async () => {
  const s=sample()
  s.preview={confirmation_id:'33333333-3333-4333-8333-333333333333',expires_at:'2099-01-01T00:00:00',changes:[],excluded_paths:[],no_change:false}
  const html=await render(s)
  assert.ok(html.includes('未保存の変更は提出されません'))
  assert.ok(html.includes('ここに表示した固定済みの内容だけを提出します'))
})
test('大量の差分を一度に全表示しない', async () => {
  const s=sample();s.diff.files=Array.from({length:1000},(_,i)=>({path:`src/file-${i}`,change:'A',patch:'+'+'x'.repeat(22000)}))
  const html=await render(s)
  assert.equal((html.match(/class="file-diff"/g)||[]).length,40);assert.ok(html.includes('次の40ファイル'));assert.ok(html.length<1000000)
})

test('提出前の確認差分もHTMLを実行せず描画する', async () => {
  const s=sample();s.preview={diff:{files:[{path:'src/main.py',patch:'<script>synthetic</script>',binary:false}],truncated:false}}
  const html=await render(s)
  assert.ok(html.includes('提出前のコード差分'));assert.ok(html.includes('&lt;script&gt;synthetic'))
  assert.ok(!html.includes('<script>synthetic</script>'))
})
test('失効・除外・変更なし・期限切れを表示する', async () => {
  const s=sample();s.basis.state='REVOKED';s.preview={no_change:true,changes:[],excluded_paths:['src/.env'],expires_at:'2000-01-01T00:00:00',excluded_policy_version:1,main_differs:true}
  const html=await render(s)
  for(const word of ['作業基準の失効','変更なし','期限切れ','src/.env','自動統合は行いません'])assert.ok(html.includes(word))
})
test('合成データ専用の静的確認用HTMLを生成する', async () => {
  const html=await render(sample())
  const source=await readFile(new URL('../src/components/CodeSharingPanel.vue',import.meta.url),'utf8')
  const css=source.match(/<style scoped>([\s\S]*?)<\/style>/)[1]
  const directory=new URL('../artifacts/',import.meta.url);await mkdir(directory,{recursive:true})
  await writeFile(new URL('code-sharing-synthetic.html',directory),'<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>合成表示の確認</title><style>'+css+'</style><p>合成データの静的表示です。操作は動作しません。実環境の成功を示すものではありません。</p>'+html+'</html>')
})
