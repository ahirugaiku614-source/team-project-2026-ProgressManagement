import { ApiError, explain } from './api.mjs'

export const positiveId = value => /^[1-9]\d*$/.test(String(value ?? '')) && Number.isSafeInteger(Number(value)) ? Number(value) : null
const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
// 既存バックエンドのnaive datetimeはUTCで保存される。ブラウザーの現地時刻として解釈しない。
export const serverTime = value => typeof value === 'string' ? Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z') : NaN
export const reviewLabel = row => row.integration?.status === 'COMPLETED' ? '統合済み'
  : row.integration?.status === 'PREPARED' || row.integrationUnknown ? '確認が必要'
    : ({ PENDING: 'レビュー待ち', APPROVED: '承認済み', CHANGES_REQUESTED: '修正が必要' }[row.review_decision] || '状態未取得')

export function initialState() {
  return { me: null, teams: [], projects: [], tasks: [], teamId: null, projectId: null, taskId: null,
    membership: false, reviews: [], offset: 0, more: false, detail: null, diff: null, detailError: '',
    loading: false, detailLoading: false, busy: false, operation: null, reviewUncertain: false, message: '', error: '', filter: 'すべて',
    basis: null, environment: null, candidates: [], selectedDeletes: [], deletionIds: [], preview: null,
    journal: null, result: null, title: '', description: '', previousRequestId: null, reason: '',
    sessionConnected: false, visibleFiles: 40 }
}

export function permissions(s) {
  const row = s.detail
  const known = s.membership && Number.isInteger(s.me?.id) && s.diff && !s.detailError && row?.project_id === s.projectId && Number.isInteger(row?.submitted_by)
  const free = !s.busy && !s.reviewUncertain && !s.detailLoading
  return {
    review: !!(known && free && row.state === 'OPEN' && row.review_decision === 'PENDING' && row.submitted_by !== s.me.id),
    integrate: !!(known && free && row.state === 'OPEN' && row.review_decision === 'APPROVED'
      && Number.isInteger(row.reviewer_id) && row.reviewer_id !== row.submitted_by && row.integration === null),
  }
}

export function createWorkflow({ request, state = initialState(), storage = null, now = () => Date.now(),
  makeId = () => crypto.randomUUID(), confirm = () => false, sessionSource = null } = {}) {
  // 本番の編集セッション取得契約はまだない。テストの明示注入以外では収集を許可しない。
  // セッションIDを手入力する画面や、URLから模擬機能へ切り替える機能は作らない。
  const s = state; s.sessionConnected = !!sessionSource
  let managedSession = null
  // コンテキストと読取り操作ごとの世代を持ち、遅れた応答による画面の巻戻りを防止する。
  // 書込みは単一ラッチで直列化し、画面切替をサーバー操作の取消しとは解釈しない。
  let epoch = 0, detailEpoch = 0, listEpoch = 0, workEpoch = 0, controller = new AbortController(), mutationLock = false
  const projectPath = () => `/api/teams/${s.teamId}/projects/${s.projectId}`
  const taskPath = () => `${projectPath()}/tasks/${s.taskId}`
  const workPath = () => `${taskPath()}/work-bases/${s.basis.id}`
  const key = () => `changes:v1:${s.me?.id}:${s.teamId}:${s.projectId}:${s.taskId}`
  const reviewKey = id => `review:v1:${s.me?.id}:${s.teamId}:${s.projectId}:${id}`
  const get = path => request(path, { signal: controller.signal })
  function save(k, value) {
    // 本文・差分・許可値・認証情報は保存しない。応答不明時の重複防止用IDだけを保持する。
    try {
      if (!storage) throw new Error('保持先未接続')
      value ? storage.setItem(k, JSON.stringify(value)) : storage.removeItem(k)
    } catch { throw new ApiError(503, 'OPERATION_RECORD_UNAVAILABLE') }
  }
  function read(k) { try { const value = storage?.getItem(k) || 'null'; return value.length > 4096 ? { invalid: true } : JSON.parse(value) } catch { return { invalid: true } } }
  function clearWork() {
    managedSession = null; s.sessionConnected = !!sessionSource
    s.editorUrl = null
    Object.assign(s, { basis: null, environment: null, candidates: [], selectedDeletes: [], deletionIds: [], preview: null,
      journal: null, result: null, title: '', description: '', previousRequestId: null })
  }
  function reset() {
    epoch++; detailEpoch++; listEpoch++; workEpoch++; controller.abort(); controller = new AbortController()
    Object.assign(s, { error: '', message: '', detail: null, diff: null, detailError: '', reviewUncertain: false,
      detailLoading: false, loading: false, busy: mutationLock, visibleFiles: 40 })
    clearWork()
  }
  function restore() {
    const j = read(key())
    if (j && [null,'START','COLLECTION','SUBMIT','DELETE'].includes(j.pending) && (uuid(j.basis_id) || (j.pending === 'START' && j.basis_id === null)) && (!j.submission_id || uuid(j.submission_id)) && (!j.request_id || uuid(j.request_id))) {
      s.journal = { basis_id: j.basis_id, submission_id: j.submission_id || null, request_id: j.request_id || null,
        pending: ['START', 'COLLECTION', 'SUBMIT', 'DELETE'].includes(j.pending) ? j.pending : null }
      if (j.pending === 'START') s.error = '作業開始の結果が不明です。現在の基準を確認し、管理者に確認してください。'
      else if (s.basis?.id !== j.basis_id) s.error = '保持した操作は別の作業基準です。新しい操作をせず管理者に確認してください。'
    } else if (j !== null) { s.journal = { basis_id: s.basis?.id || null, pending: 'INVALID' }; s.error = '保持した操作情報を確認できません。新しいIDで操作せず管理者に確認してください。' }
  }
  async function bootstrap() {
    reset(); Object.assign(s, { me: null, teamId: null, projectId: null, taskId: null, teams: [], projects: [], tasks: [], reviews: [], membership: false })
    const e = epoch; s.loading = true
    try {
      const [me, teams] = await Promise.all([get('/api/me'), get('/api/teams')])
      if (e !== epoch) return
      if (!Number.isInteger(me?.id) || !Array.isArray(teams)) throw new ApiError(502,'RESPONSE_INVALID')
      s.me = me; s.teams = teams
    } catch (error) { if (e === epoch) s.error = explain(error) }
    finally { if (e === epoch) s.loading = false }
  }
  async function selectTeam(id) {
    reset(); s.teamId = positiveId(id); s.projectId = null; s.taskId = null
    Object.assign(s, { projects: [], tasks: [], reviews: [], membership: false })
    if (!s.teamId || !s.me) return
    const e = epoch; s.loading = true
    try {
      const base = `/api/teams/${s.teamId}`
      const [projects, tasks, members] = await Promise.all([get(base + '/projects'), get(base + '/tasks'), get(base + '/members')])
      if (e !== epoch) return
      s.projects = projects; s.tasks = tasks.filter(t => t.assignee_id === s.me.id)
      s.membership = members.some(m => m.user_id === s.me.id)
    } catch (error) { if (e === epoch) s.error = explain(error) }
    finally { if (e === epoch) s.loading = false }
  }
  async function selectProject(id) {
    reset(); s.projectId = positiveId(id); s.taskId = null; s.reviews = []; s.offset = 0
    if (!s.projectId || !s.projects.some(p => p.id === s.projectId)) { s.projectId = null; return }
    await loadReviews(0)
  }
  async function loadReviews(offset = s.offset) {
    if (!s.projectId || !s.membership) return
    const e = epoch, list = ++listEpoch, projectId = s.projectId; const path = projectPath(); s.loading = true; s.error = ''
    try {
      const rows = await get(`${path}/change-requests?limit=10&offset=${Math.max(0, offset)}`)
      if (!Array.isArray(rows) || rows.some(r=>r.project_id!==projectId || !positiveId(r.id))) throw new ApiError(502,'RESPONSE_INVALID')
      const enriched = await Promise.all(rows.map(async row => {
        try { return { ...row, integration: await get(`${path}/change-requests/${row.id}/integration`) } }
        catch { return { ...row, integration: undefined, integrationUnknown: true } }
      }))
      if (e !== epoch || list !== listEpoch) return
      s.reviews = enriched; s.offset = offset; s.more = rows.length === 10
    } catch (error) { if (e === epoch && list === listEpoch) s.error = explain(error) }
    finally { if (e === epoch && list === listEpoch) s.loading = false }
  }
  async function selectTask(id) {
    reset(); s.taskId = positiveId(id)
    if (!s.projectId || !s.tasks.some(t => t.id === s.taskId)) { s.taskId = null; return }
    await refreshWork()
  }
  async function refreshWork() {
    if (!s.taskId || !s.projectId) return
    const e = epoch, work = ++workEpoch; s.loading = true
    try {
      const base = taskPath()
      const [current, environment] = await Promise.all([get(base + '/work-bases/current'), get(projectPath() + '/editor')])
      if (e !== epoch || work !== workEpoch) return
      if (s.basis?.id !== current.basis?.id) {
        // 新世代へ旧session・確認ID・接続リンクを持ち越さない。旧環境自体は削除しない。
        managedSession = null; s.sessionConnected = !!sessionSource
        s.preview = null; s.editorUrl = null; s.result = null
      }
      s.basis = current.basis; s.environment = environment; restore()
    } catch (error) { if (e === epoch && work === workEpoch) { s.error = explain(error); s.preview = null; s.basis = null; s.environment = null } }
    finally { if (e === epoch && work === workEpoch) s.loading = false }
  }
  async function mutate(kind, action) {
    if (mutationLock || !s.membership) return
    mutationLock = true; s.busy = true; s.operation = kind; s.error = ''; s.message = ''
    const e = epoch
    try { await action(e) } catch (error) {
      if (e === epoch) { s.error = explain(error); if (['PREVIEW_CHANGED','PREVIEW_EXPIRED','WORK_BASIS_INACTIVE'].includes(error.code)) s.preview = null }
    } finally { mutationLock = false; s.busy = false; s.operation = null }
  }
  const active = () => s.basis?.state === 'ACTIVE' && !s.loading && (!s.journal || s.journal.basis_id === s.basis.id)
  async function start() {
    if (!s.taskId || s.basis?.state === 'ACTIVE' || s.journal?.pending) return
    const path = taskPath() + '/work-bases', k = key()
    await mutate('START', async e => {
      save(k, { basis_id: null, request_id: null, submission_id: null, pending: 'START' })
      s.journal = { basis_id: null, request_id: null, submission_id: null, pending: 'START' }
      const row = await request(path, { method: 'POST', body: {} })
      if (!uuid(row.id) || row.state !== 'ACTIVE') throw new ApiError(502,'RESPONSE_INVALID')
      save(k, null)
      if (e !== epoch) return
      s.basis = row; s.journal = null; s.message = row.state === 'ACTIVE' ? '作業基準を登録しました。作業環境と保存状態は別途確認してください。' : '作業基準の状態を確認してください。'
    })
  }
  function invalidatePreview() { s.preview = null }
  async function refreshEnvironment() {
    if (!s.taskId) return
    const e = epoch
    try {
      const environment = await get(taskPath() + '/work-bases/environment')
      if (e === epoch) s.environment = environment
    } catch (error) { if (e === epoch) s.error = explain(error) }
  }
  async function connectSession() {
    if (!active()) return
    const path = workPath()
    await mutate('SESSION', async e => {
      const result = await request(path + '/editing-sessions', { method: 'POST', body: {} })
      if (e !== epoch) return
      if (!uuid(result.session_id) || !Number.isFinite(serverTime(result.expires_at)) || serverTime(result.expires_at) <= now()) throw new ApiError(502, 'RESPONSE_INVALID')
      managedSession = result.session_id; s.sessionConnected = true
      s.message = '管理側で編集セッションを確認しました。未保存の変更は提出対象に含まれません。'
    })
  }
  async function openEditor() {
    if (!active() || !managedSession) return
    await mutate('EDITOR_ACCESS', async e => {
      const result = await request(workPath() + '/editor-access', { method: 'POST', body: { session_id: managedSession } })
      if (e !== epoch) return
      if (!uuid(s.basis?.workspace_id) || result.url !== '/code-server/' + s.basis.workspace_id + '/') throw new ApiError(502, 'RESPONSE_INVALID')
      s.editorUrl = result.url
      s.message = '下の接続リンクを開いてください。リンクの権限は短時間で失効します。'
    })
  }
  async function prepareEnvironment(latest = false) {
    if (!s.taskId || (latest && (!active() || !s.sessionConnected)) || (!latest && s.basis?.state === 'ACTIVE')) return
    const path = taskPath() + '/work-bases/environment', k = key() + ':environment'
    const previous = latest ? s.basis.id : null
    await mutate('ENVIRONMENT', async e => {
      // 応答不明の準備操作は同じIDで照会する。別IDの自動再実行はしない。
      if (read(k)) { s.error = '準備操作の記録があります。先に準備結果を確認してください。'; return }
      const request_id = makeId()
      save(k, { request_id, previous_basis_id: previous })
      const result = await request(path, { method: 'POST', body: { request_id,
        previous_basis_id: previous, session_id: latest ? (sessionSource ? await sessionSource() : managedSession) : null } })
      if (e !== epoch) return
      if (result.state !== 'CONFIRMED' || !uuid(result.basis_id)) {
        s.error = '作業環境の準備結果が不明です。再実行せず結果を確認してください。'; return
      }
      save(k, null); await refreshWork()
      if (e === epoch) s.message = '新しい作業環境を準備しました。以前の環境は上書きしていません。'
    })
  }
  async function checkEnvironment() {
    const record = read(key() + ':environment'), e = epoch
    if (!uuid(record?.request_id)) { s.error = '照会する準備操作はありません。'; return }
    try {
      const result = await get(taskPath() + '/work-bases/environment/requests/' + record.request_id)
      if (e !== epoch) return
      if (result.state === 'CONFIRMED' && uuid(result.basis_id)) {
        save(key() + ':environment', null); await refreshWork()
      } else s.error = '準備結果は未確定です。記録なしは未実行の証明になりません。管理者に確認してください。'
    } catch (error) { if (e === epoch) s.error = explain(error) }
  }
  async function checkConfirmation() {
    if (!uuid(s.preview?.confirmation_id)) return
    const e = epoch
    try {
      const result = await get(workPath() + '/confirmations/' + s.preview.confirmation_id)
      if (e !== epoch) return
      if (result.state !== 'AVAILABLE') { s.preview = null; s.error = '変更確認は期限切れ・失効・使用済みです。もう一度確認してください。' }
      else s.message = '変更確認は有効です。最終提出時にも内容を照合します。'
    } catch (error) { if (e === epoch) s.error = explain(error) }
  }
  async function collect() {
    if (!active() || (!sessionSource && !managedSession) || s.journal?.request_id || s.journal?.pending) return
    const base = taskPath(), path = workPath(), basisId = s.basis.id, k = key()
    await mutate('COLLECTION', async e => {
      const sessionId = sessionSource ? await sessionSource() : managedSession
      if (e !== epoch) return
      if (!uuid(sessionId)) throw new ApiError(503, 'EDIT_SESSION_UNVERIFIED')
      const j = { basis_id: basisId, request_id: makeId(), submission_id: null, pending: 'COLLECTION' }
      s.journal = j; save(k, j)
      // 方式Cはdirty=falseを申告しない。管理側のセッション認可を通して、
      // ディスク上の内容だけを新しい不変スナップショットとして固定する。
      const permit = await request(base + '/submission-permits', { method: 'POST', body: { editing_session_id: sessionId } })
      const result = await request(path + '/collections', { method: 'POST', body: {
        editing_session_id: sessionId, request_id: j.request_id, permit: permit.permit,
      } })
      if (!uuid(result.submission_id) || !['SUBMITTED','PROCESSING','REVIEW_REQUIRED'].includes(result.collection_state)) throw new ApiError(502, 'RESPONSE_INVALID')
      j.submission_id = result.submission_id; j.pending = null; save(k, j)
      if (e !== epoch) return
      s.journal = { ...j }; s.result = result
      s.message = result.collection_state === 'SUBMITTED' ? '保存済みファイルを収集しました。レビュー依頼はまだ作成していません。' : explanationsForCollection(result)
    })
  }
  function explanationsForCollection(result) { return result.error_code ? explain({ code: result.error_code }) : '収集の結果を確認してください。' }
  async function checkResult() {
    const j = s.journal
    if (j?.request_id && !j.submission_id && j.basis_id === s.basis?.id) {
      const e = epoch, k = key()
      try {
        const found = await get(workPath() + '/requests/' + j.request_id)
        if (e !== epoch) return
        if (found.outcome !== 'RECORDED' || !uuid(found.submission_id)) {
          s.error = '要求の結果は不明です。記録なしは未実行を意味しません。別IDで再提出しないでください。'; return
        }
        j.submission_id = found.submission_id; j.pending = null
        save(k, j); s.journal = { ...j }; s.result = found
        s.message = '同じ操作IDに対応する記録を確認しました。再収集・再提出は行っていません。'
      } catch (error) { if (e === epoch) s.error = explain(error); return }
    }
    if (!j?.submission_id || j.basis_id !== s.basis?.id) {
      s.error = '結果を照会する収集IDがありません。応答不明の操作を新しいIDでやり直さず、管理者に確認してください。'; return
    }
    const e = epoch
    try {
      const result = await get(workPath() + '/collections/' + j.submission_id)
      if (e !== epoch) return
      s.result = result
      if (['READY','NO_CHANGE'].includes(result.conversion_state)) { j.pending = null; save(key(), j) }
      s.message = result.conversion_state === 'READY' && positiveId(result.change_request_id) ? 'レビュー依頼の作成を確認しました。承認・反映の状態は別に確認してください。'
        : result.conversion_state === 'NO_CHANGE' ? '変更なしとして確定しました。レビュー依頼は作成していません。'
          : '記録された結果を表示しました。未確定の操作は自動再送しません。'
    } catch (error) { if (e === epoch) s.error = explain(error) }
  }
  async function loadCandidates() {
    if (!active() || !s.journal?.submission_id) return
    const e = epoch
    try {
      const data = await get(workPath() + '/deletion-candidates')
      if (!Array.isArray(data.candidates) || data.candidates.length>1000 || data.candidates.some(c=>!uuid(c?.id) || typeof c.path!=='string')) throw new ApiError(502,'RESPONSE_INVALID')
      if (e === epoch) s.candidates = data.candidates
    }
    catch (error) { if (e === epoch) s.error = explain(error) }
  }
  async function confirmDeletions() {
    if (!active() || !s.journal?.submission_id || s.journal.pending || !s.selectedDeletes.length) return
    const chosen = s.selectedDeletes.filter(id => s.candidates.some(c => c.id === id))
    if (chosen.length !== s.selectedDeletes.length || !confirm('選択したファイルの削除を変更内容に含めますか？')) return
    const path = workPath(), sid = s.journal.submission_id, k = key(), j = { ...s.journal, pending: 'DELETE' }
    await mutate('DELETE', async e => {
      s.journal = j; save(k, j)
      const ids = []
      for (const candidate_id of chosen) {
        const row = await request(path + '/deletion-intents', { method: 'POST', body: { submission_id: sid, candidate_id, confirmation_id: makeId() } })
        ids.push(row.deletion_id)
      }
      j.pending = null; save(k, j)
      if (e !== epoch) return
      s.journal = { ...j }
      s.deletionIds = [...new Set([...s.deletionIds, ...ids])]; s.selectedDeletes = []; s.preview = null
      s.message = '削除意図を登録しました。変更をもう一度確認してください。'
    })
  }
  function payload() {
    return { submission_id: s.journal.submission_id, title: s.title, description: s.description || null,
      deletion_ids: [...s.deletionIds], previous_request_id: s.previousRequestId }
  }
  const signature = () => JSON.stringify(payload())
  const canPreview = () => active() && s.sessionConnected && s.result?.collection_state === 'SUBMITTED'
    && !['READY','NO_CHANGE','PREPARED','REVIEW_REQUIRED'].includes(s.result?.conversion_state)
    && !!s.journal?.submission_id && !s.journal.pending && !!s.title.trim() && !s.busy
  const canSubmit = () => canPreview() && !!s.preview && signature() === s.preview.signature
    && Number.isFinite(serverTime(s.preview.expires_at)) && serverTime(s.preview.expires_at) > now()
  async function preview() {
    if (!canPreview()) return
    const body = payload(), sig = signature(), path = workPath()
    await mutate('PREVIEW', async e => {
      s.preview = null
      const result = await request(path + '/preview', { method: 'POST', body })
      if (!uuid(result.confirmation_id) || !Number.isFinite(serverTime(result.expires_at)) || typeof result.no_change !== 'boolean'
        || !Array.isArray(result.changes) || result.changes.length > 1000
        || result.changes.some(f=>!f || typeof f.path!=='string' || !['ADDED','MODIFIED','DELETED'].includes(f.change))
        || !Array.isArray(result.excluded_paths) || result.excluded_paths.some(p=>typeof p!=='string')) throw new ApiError(502,'RESPONSE_INVALID')
      if (e !== epoch || sig !== signature()) return
      s.preview = { ...result, signature: sig }; s.message = result.no_change ? '変更はありません。確定してもレビュー依頼は作成しません。' : '変更確認済みです。提出操作はまだ行っていません。'
    })
  }
  async function submit() {
    if (mutationLock) return
    if (!canSubmit()) { s.preview = null; s.error = '変更内容と有効期限を確認し、もう一度変更を確認してください。'; return }
    const body = { ...payload(), confirmation_id: s.preview.confirmation_id }, path = workPath(), k = key()
    const j = { ...s.journal, pending: 'SUBMIT' }
    await mutate('SUBMIT', async e => {
      s.journal = j; save(k, j)
      let result
      try { result = await request(path + '/change-request', { method: 'POST', body }) }
      catch (error) {
        // 記録前に確定した拒否だけは再確認を許可する。通信不明・DB/Git障害ではラッチを保持する。
        if (['PREVIEW_CHANGED','PREVIEW_EXPIRED','UNSAVED_CHANGES','INVALID_REQUEST','WORK_BASIS_INACTIVE'].includes(error.code)) {
          j.pending = null; save(k, j); if (e === epoch) { s.journal = { ...j }; s.preview = null }
        }
        throw error
      }
      if (!['READY','NO_CHANGE'].includes(result.conversion_state)) throw new ApiError(502, 'RESPONSE_INVALID')
      if ((result.conversion_state === 'READY' && !positiveId(result.change_request_id))
        || (result.conversion_state === 'NO_CHANGE' && result.change_request_id !== null)) throw new ApiError(502, 'RESPONSE_INVALID')
      j.pending = null; save(k, j)
      if (e !== epoch) return
      s.journal = { ...j }; s.result = result; s.preview = null
      s.message = result.change_request_id ? 'レビュー依頼を作成しました。まだ承認・反映は行っていません。' : '変更なしとして確定しました。レビュー依頼は作成していません。'
      await loadReviews()
    })
  }
  async function openReview(id) {
    const d = ++detailEpoch, e = epoch, path = projectPath() + '/change-requests/' + positiveId(id)
    s.detail = null; s.diff = null; s.detailError = ''; s.detailLoading = true; s.reason = ''; s.visibleFiles = 40
    try {
      const [row, integration] = await Promise.all([get(path), get(path + '/integration')])
      if (e !== epoch || d !== detailEpoch) return
      if (row.id !== positiveId(id) || row.project_id !== s.projectId) throw new ApiError(502, 'RESPONSE_INVALID')
      s.detail = { ...row, integration }; s.reviewUncertain = !!read(reviewKey(row.id))
      // 既存依頼にタスク結合がない場合も、架空の担当タスクを補わない。
      try {
        const context = await get(path + '/task-context')
        if (e === epoch && d === detailEpoch) Object.assign(s.detail, { task_context: context })
      } catch { /* 古いAPIとの互換性を保ち、取得不能は「未取得」と表示する。 */ }
      if (e !== epoch || d !== detailEpoch) return
      // 読取りだけで未確定操作を再送しない。確定状態が観測できた場合だけラッチを解く。
      if (row.review_decision !== 'PENDING' && integration?.status !== 'PREPARED') {
        const pending = read(reviewKey(row.id))
        if (pending && !pending.invalid && (pending.kind !== 'integrate' || integration?.status === 'COMPLETED')) { save(reviewKey(row.id), null); s.reviewUncertain = false }
      }
      try {
        const diff = await get(path + '/diff')
        if (diff.base_commit !== row.base_commit || diff.snapshot_commit !== row.snapshot_commit || !Array.isArray(diff.files)
          || diff.files.length>1000 || diff.files.some(f=>!f || typeof f.path!=='string' || !['A','M','D'].includes(f.change)
            || (f.patch!==null && typeof f.patch!=='string'))) throw new ApiError(502, 'RESPONSE_INVALID')
        if (e === epoch && d === detailEpoch) s.diff = diff
      } catch { if (e === epoch && d === detailEpoch) s.detailError = '変更内容を取得できません。本文を表示せず、もう一度詳細を確認してください。' }
    } catch (error) { if (e === epoch && d === detailEpoch) s.detailError = explain(error) }
    finally { if (e === epoch && d === detailEpoch) s.detailLoading = false }
  }
  async function reviewAction(kind) {
    const p = permissions(s)
    if (!(['approve','request-changes'].includes(kind) ? p.review : kind === 'integrate' && p.integrate)) return
    if (kind === 'request-changes' && !s.reason.trim()) { s.error = '修正を依頼する理由を入力してください。'; return }
    if (!confirm(kind === 'integrate' ? '承認済みの変更をチームの共有コードへ反映しますか？' : kind === 'approve' ? 'この変更を承認しますか？' : '理由を添えて修正を依頼しますか？')) return
    const id = s.detail.id, projectId = s.projectId, path = projectPath() + '/change-requests/' + id, k = reviewKey(id)
    const body = kind === 'request-changes' ? { reason: s.reason.trim() } : {}
    await mutate(kind, async e => {
      save(k, { kind }); s.reviewUncertain = true
      const result = await request(path + '/' + kind, { method: 'POST', body })
      if (kind === 'integrate' ? result.status !== 'COMPLETED' || result.change_request_id !== id || result.project_id !== projectId
        : result.id !== id || result.project_id !== projectId || result.review_decision !== (kind === 'approve' ? 'APPROVED' : 'CHANGES_REQUESTED')) throw new ApiError(502, 'RESPONSE_INVALID')
      save(k, null)
      if (e !== epoch) return
      s.reviewUncertain = false
      s.message = kind === 'integrate' ? 'チームの共有コードへの反映を確認しました。' : kind === 'approve' ? '承認しました。チームへの反映は別の操作です。' : '修正を依頼しました。既存の変更内容は保持されます。'
      await openReview(id); await loadReviews()
    })
  }
  function nextChange() {
    if (!['READY','NO_CHANGE'].includes(s.result?.conversion_state) || s.journal?.pending || s.busy) return
    if (!confirm('前の収集結果を保持したまま、次の変更の準備を始めますか？')) return
    s.previousRequestId = s.result.change_request_id || null
    s.journal = null; save(key(), null); s.result = null; s.preview = null; s.candidates = []; s.deletionIds = []; s.selectedDeletes = []
  }
  function dispose() { reset() }
  return { state: s, bootstrap, selectTeam, selectProject, selectTask, refreshWork, loadReviews, openReview,
    refreshEnvironment, connectSession, openEditor, prepareEnvironment, checkEnvironment, checkConfirmation,
    start, collect, checkResult, loadCandidates, confirmDeletions, preview, submit, reviewAction,
    invalidatePreview, canPreview, canSubmit, active, nextChange, dispose }
}
