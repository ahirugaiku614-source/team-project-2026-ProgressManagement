import {createApp, reactive, ref, h} from 'vue'
import CodeSharingPanel from '../components/CodeSharingPanel.vue'
import {createWorkflow,initialState} from '../code-sharing/workflow.mjs'
import {syntheticApi} from './synthetic-api.mjs'

// この入口だけが合成APIを明示注入する。通常のログイン・Firebaseへ接続しない。
const api=syntheticApi(), state=reactive(initialState()), draft=ref(''), actor=ref(1)
const memory=new Map(), storage={getItem:key=>memory.get(key)||null,setItem:(key,value)=>memory.set(key,value),removeItem:key=>memory.delete(key)}
const model=createWorkflow({request:api.request,state,storage,confirm:message=>window.confirm(message)})
async function select(value){
  actor.value=value;api.selectActor(value);model.dispose();await model.bootstrap();await model.selectTeam(1);await model.selectProject(2);await model.selectTask(value===1?3:4);draft.value=api.current().draft
}
async function choose(kind,value){if(kind==='team')await model.selectTeam(value);if(kind==='project')await model.selectProject(value);if(kind==='task')await model.selectTask(value)}
createApp({setup(){return()=>h('div',[
  h('section',{style:'padding:20px;background:#fff5d6;color:#243449;font:16px system-ui'},[
    h('h1','合成データ専用デモ（実code-server・DB・Firebaseとは接続しません）'),
    h('p','Aで準備→編集・保存→セッション確認→収集→確認→提出。Bでレビュー・承認→チームに反映。最新版取得は新しい環境を作ります。'),
    h('button',{onClick:()=>select(1)},'メンバーAに切替'),h('button',{onClick:()=>select(2)},'メンバーBに切替'),
    h('p','編集パネルはcode-serverの代替デモです。プログラムとして実行しません。'),
    h('textarea',{value:draft.value,rows:5,style:'display:block;width:95%;font:16px monospace',onInput:event=>{draft.value=event.target.value;api.draft(draft.value)}}),
    h('button',{onClick:()=>api.save()},'合成ファイルを保存（提出しません）'),
    h('button',{onClick:()=>{draft.value=api.current().draft}},'新しい環境の内容を表示'),
  ]),h(CodeSharingPanel,{state,model,onChoose:choose})
])}}).mount('#app')
select(1)
