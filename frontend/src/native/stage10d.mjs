import {createApp,reactive,ref,h} from 'vue'
import CodeSharingPanel from '../components/CodeSharingPanel.vue'
import {createWorkflow,initialState} from '../code-sharing/workflow.mjs'
import {createApi} from '../code-sharing/api.mjs'
import {syntheticLogin} from './auth.mjs'
import '../style.css'

// memory APIを使わず、同一originの実PostgreSQL/Git/UDS APIへ接続する専用build。
// 画面のA/B選択は認証結果ではない。サーバーがそれぞれのpasswordを検証する。
const auth=syntheticLogin(),state=reactive(initialState()),account=ref('A'),password=ref('')
const logged=ref(false),busy=ref(false),message=ref(''),uncertain=ref(false)
let storage=null
try { storage=window.sessionStorage } catch { /* 操作IDを安全に保持できなければ製品modelが変更操作を拒否する。 */ }
const model=createWorkflow({state,request:createApi({getToken:auth.getToken}),storage,confirm:text=>window.confirm(text)})
async function login(){
  if(busy.value)return
  busy.value=true;message.value=''
  try {
    await auth.login(account.value,password.value);await model.bootstrap();logged.value=!!state.me
    if(logged.value)uncertain.value=false
    else message.value='製品APIの本人情報を確認できません。接続状態を確認してください。'
  }
  catch { message.value='ログインできません。専用の合成資格情報と接続状態を確認してください。' }
  finally { password.value='';busy.value=false }
}
async function logout(){
  if(busy.value)return
  busy.value=true;uncertain.value=true
  try { await auth.logout();model.dispose();logged.value=false;state.me=null;message.value='ログアウトしました。編集接続も失効します。' }
  catch { message.value='ログアウト結果が不明です。変更操作を止め、もう一度ログアウトしてください。' }
  finally { busy.value=false }
}
async function choose(kind,value){if(kind==='team')await model.selectTeam(value);if(kind==='project')await model.selectProject(value);if(kind==='task')await model.selectTask(value)}
createApp({setup(){return()=>h('div',[
  h('section',{style:'padding:18px;background:#fff5d6;color:#243449'},[
    h('h1','合成認証・合成データ専用の実機MVP'),
    h('p','実Firebaseは使いません。保存済み内容を固定して確認し、明示操作した内容だけを提出します。'),
    h('p','未保存の変更は含まれません。既存の本番環境・デモvolumeを使わないでください。'),
    logged.value?h('button',{disabled:busy.value,onClick:logout},'合成セッションからログアウト'):h('form',{onSubmit:e=>{e.preventDefault();login()}},[
      h('label',['合成アカウント ',h('select',{value:account.value,onChange:e=>account.value=e.target.value},[h('option',{value:'A'},'メンバーA'),h('option',{value:'B'},'メンバーB')])]),
      h('input',{type:'password',autocomplete:'off',value:password.value,placeholder:'管理者が発行した専用password',onInput:e=>password.value=e.target.value}),
      h('button',{type:'submit',disabled:busy.value},'合成環境へログイン')]),
    h('p',{role:'status'},message.value)]),
  logged.value&&!uncertain.value?h(CodeSharingPanel,{state,model,onChoose:choose}):h('p',uncertain.value?'認証状態を確認できないため変更操作を停止しています。':'ログイン後にチーム・プロジェクト・担当タスクを選びます。')
])}}).mount('#app')
