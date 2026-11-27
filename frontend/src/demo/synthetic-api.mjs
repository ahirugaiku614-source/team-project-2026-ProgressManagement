// 合成デモ専用。通常画面からimportせず、通信失敗時の代替にも使用しない。
// ソース文字列はメモリ内で扱うだけで、実行・外部送信・端末保存は行わない。
export function syntheticApi() {
  let actor=1, sequence=0, main='print("Hello team")\n', version='1'.repeat(40)
  const users={1:{id:1,name:'メンバーA'},2:{id:2,name:'メンバーB'}}
  const work={}, requests=new Map(), captures=new Map(), confirmations=new Map(), reviews=[]
  const id=()=>`90000000-0000-4000-8000-${String(++sequence).padStart(12,'0')}`
  const error=code=>{const e=new Error('合成操作を完了できません');e.code=code;throw e}
  const current=()=>work[actor] ||= {saved:main,draft:main,basis:null}
  const result=(capture)=>({submission_id:capture.id,collection_state:'SUBMITTED',conversion_state:capture.review?'READY':'NOT_REQUESTED',change_request_id:capture.review?.id||null})
  async function request(path,{method='GET',body={}}={}) {
    const w=current()
    if(path==='/api/me')return users[actor]
    if(path==='/api/teams')return [{id:1,name:'合成デモチーム'}]
    if(path==='/api/teams/1/projects')return [{id:2,name:'共同開発MVP'}]
    if(path==='/api/teams/1/members')return [{user_id:1},{user_id:2}]
    if(path==='/api/teams/1/tasks')return [{id:3,title:'表示処理の改善',assignee_id:1,status:'IN_PROGRESS'},{id:4,title:'動作確認とレビュー',assignee_id:2,status:'IN_PROGRESS'}]
    if(path.endsWith('/editor'))return {state:'UNAVAILABLE',launch_enabled:false}
    if(path.endsWith('/environment') && method==='GET')return {state:'CONFIGURED',prepare_enabled:true,launch_enabled:false}
    if(path.endsWith('/environment') && method==='POST') {
      if(body.previous_basis_id && (w.draft!==w.saved || w.saved!==w.base))error(w.draft!==w.saved?'UNSAVED_CHANGES':'UNSUBMITTED_CHANGES')
      if(requests.has(body.request_id))return requests.get(body.request_id)
      w.base=main;w.saved=main;w.draft=main;w.basis={id:id(),state:'ACTIVE',workspace_id:id(),base_version:version,started_at:new Date().toISOString()};w.version=version
      const r={state:'CONFIRMED',basis_id:w.basis.id};requests.set(body.request_id,r);return r
    }
    if(path.endsWith('/work-bases/current'))return {basis:w.basis}
    if(path.endsWith('/editing-sessions'))return {session_id:id(),expires_at:'2099-01-01T00:00:00Z'}
    if(path.endsWith('/editor-access'))error('EDITOR_GATEWAY_UNAVAILABLE')
    if(path.endsWith('/submission-permits'))return {permit:'synthetic-permit-not-a-real-credential'}
    if(path.endsWith('/collections') && method==='POST') {
      if(w.draft!==w.saved)error('UNSAVED_CHANGES')
      const capture={id:id(),actor,basis:w.basis.id,saved:w.saved,base:w.base,version:w.version}
      captures.set(capture.id,capture);const r={...result(capture),outcome:'RECORDED'};requests.set(body.request_id,r);return r
    }
    if(path.includes('/requests/'))return requests.get(path.split('/').pop())||{outcome:'UNKNOWN',state:'UNKNOWN'}
    if(path.includes('/collections/')){const c=captures.get(path.split('/').pop());if(!c || c.actor!==actor)error('REVIEW_FORBIDDEN');return result(c)}
    if(path.endsWith('/deletion-candidates'))return {candidates:[]}
    if(path.endsWith('/preview')) {
      const c=captures.get(body.submission_id);if(!c || c.actor!==actor)error('REVIEW_FORBIDDEN')
      const confirmation_id=id();confirmations.set(confirmation_id,JSON.stringify(body))
      return {confirmation_id,expires_at:'2099-01-01T00:00:00Z',no_change:c.saved===c.base,
        diff:{files:c.saved===c.base?[]:[{path:'src/main.py',change:'M',patch:'--- 保存前\n+++ 変更後\n-'+c.base+'+'+c.saved,binary:false}],truncated:false},
        changes:c.saved===c.base?[]:[{path:'src/main.py',change:'MODIFIED'}],excluded_paths:[],main_differs:c.version!==version}
    }
    if(path.endsWith('/change-request')) {
      const {confirmation_id,...input}=body
      if(confirmations.get(confirmation_id)!==JSON.stringify(input))error('PREVIEW_CHANGED')
      const c=captures.get(body.submission_id);if(!c || c.actor!==actor)error('REVIEW_FORBIDDEN')
      if(c.review)return result(c)
      if(c.saved===c.base)return {...result(c),conversion_state:'NO_CHANGE'}
      c.review={id:reviews.length+1,project_id:2,title:body.title,description:body.description,submitted_by:actor,
        submitter_name:users[actor].name,created_at:new Date().toISOString(),state:'OPEN',review_decision:'PENDING',
        reviewer_id:null,base_commit:c.version,snapshot_commit:String(reviews.length+2).repeat(40),capture:c.id,integration:null}
      reviews.push(c.review);return result(c)
    }
    if(path.includes('/change-requests?'))return reviews.map(r=>({...r}))
    const match=path.match(/\/change-requests\/(\d+)(?:\/(.*))?$/)
    if(match){
      const r=reviews.find(r=>r.id===Number(match[1]));if(!r)error('REVIEW_TARGET_NOT_FOUND')
      const c=captures.get(r.capture),action=match[2]
      if(!action)return {...r}
      if(action==='integration')return r.integration
      if(action==='task-context')return {task_id:3,task_title:'表示処理の改善',previous_request_id:null}
      if(action==='diff')return {base_commit:r.base_commit,snapshot_commit:r.snapshot_commit,files:[{path:'src/main.py',change:'M',patch:'--- 保存前\n+++ 変更後\n-'+c.base+'+'+c.saved,binary:false}]}
      if(action==='approve' || action==='request-changes'){
        if(r.submitted_by===actor || r.review_decision!=='PENDING')error('REVIEW_FORBIDDEN')
        r.review_decision=action==='approve'?'APPROVED':'CHANGES_REQUESTED';r.reviewer_id=actor;r.reviewer_name=users[actor].name;r.review_reason=body.reason;return {...r}
      }
      if(action==='integrate'){
        if(r.review_decision!=='APPROVED' || r.base_commit!==version)error('INTEGRATION_CONFLICT')
        main=c.saved;version=r.snapshot_commit;r.integration={status:'COMPLETED',change_request_id:r.id,project_id:r.project_id};return r.integration
      }
    }
    error('REVIEW_SERVICE_UNAVAILABLE')
  }
  return {request,selectActor:value=>{actor=value},draft:value=>{current().draft=value},save:()=>{current().saved=current().draft},current,shared:()=>main}
}
