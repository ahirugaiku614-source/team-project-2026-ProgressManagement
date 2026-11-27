<script setup>
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { permissions, reviewLabel, serverTime } from '../code-sharing/workflow.mjs'
const props = defineProps({ state: { type: Object, required: true }, model: { type: Object, required: true } })
defineEmits(['choose'])
const s = computed(() => props.state), permission = computed(() => permissions(s.value))
const now = ref(Date.now()); let timer
// ブラウザー時計は表示用。最終的な期限と認可は必ずバックエンドが判定する。
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 1000) })
onBeforeUnmount(() => clearInterval(timer))
const expired = computed(() => !s.value.preview || serverTime(s.value.preview.expires_at) <= now.value)
const filtered = computed(() => s.value.reviews.filter(r => s.value.filter === 'すべて' || reviewLabel(r) === s.value.filter))
const task = computed(() => s.value.tasks.find(t => t.id === s.value.taskId))
const date = value => Number.isFinite(serverTime(value)) ? new Date(serverTime(value)).toLocaleString('ja-JP') : '未取得'
const basisLabel = computed(() => ({ ACTIVE: '作業中', REVOKED: '作業基準の失効', ENDED: '作業終了', SUPERSEDED: '作業基準を更新済み' }[s.value.basis?.state] || '作業準備前'))
const changeLabel = value => ({ A: '追加', ADDED: '追加', M: '修正', MODIFIED: '修正', D: '削除', DELETED: '削除' }[value] || '未取得')
const operationLabel = computed(() => ({ START: '作業準備中', COLLECTION: '収集中', PREVIEW: '変更内容を確認中',
  SUBMIT: 'レビュー依頼作成中', DELETE: '削除意図を登録中', approve: '承認を登録中',
  'request-changes': '修正依頼を登録中', integrate: 'チームへの反映を確認中' }[s.value.operation] || '応答待ち'))
const taskProgress = computed(() => ({ TODO: '未着手', IN_PROGRESS: '作業中', DONE: '完了' }[task.value?.status] || '未取得'))
const collectionLabel = computed(() => ({ SUBMITTED: '収集済み', PROCESSING: '収集中', REVIEW_REQUIRED: '確認が必要' }[s.value.result?.collection_state] || '未取得'))
const conversionLabel = computed(() => ({ READY: 'レビュー依頼作成済み', NO_CHANGE: '変更なし', PREPARED: 'レビュー依頼作成中・結果確認が必要', REVIEW_REQUIRED: '確認が必要', NOT_REQUESTED: 'レビュー依頼は未作成' }[s.value.result?.conversion_state] || 'レビュー依頼は未確認'))
</script>

<template>
  <main class="sharing">
    <header class="page-header">
      <div><p class="eyebrow">チーム開発ワークスペース</p><h1>コード共有とレビュー</h1><p>保存・変更提出・承認・チームへの反映を、ひとつずつ確認できます。</p></div>
      <nav aria-label="関連画面"><a href="/">ホーム</a><a href="/schedule">進捗管理</a><a :href="`/Editor?team=${s.teamId || ''}&project=${s.projectId || ''}&task=${s.taskId || ''}`">既存エディタ</a></nav>
    </header>
    <section class="context card" aria-label="チームと作業対象">
      <label>チーム<select :value="s.teamId || ''" @change="$emit('choose', 'team', $event.target.value)"><option value="">選択してください</option><option v-for="t in s.teams" :key="t.id" :value="t.id">{{ t.name }}</option></select></label>
      <label>プロジェクト<select :value="s.projectId || ''" :disabled="!s.teamId || s.loading" @change="$emit('choose', 'project', $event.target.value)"><option value="">選択してください</option><option v-for="p in s.projects" :key="p.id" :value="p.id">{{ p.name }}</option></select></label>
      <label>自分の担当タスク<select :value="s.taskId || ''" :disabled="!s.projectId || s.loading" @change="$emit('choose', 'task', $event.target.value)"><option value="">選択してください</option><option v-for="t in s.tasks" :key="t.id" :value="t.id">{{ t.title }}</option></select></label>
      <p class="muted">タスクとプロジェクトの対応はサーバーで確認します。進捗とレビュー状態は別々です。</p>
    </section>
    <p v-if="s.loading" role="status">情報を読み込んでいます…</p><p v-if="s.busy" role="status">{{ operationLabel }}。重ねて実行しないでください。</p>
    <p v-if="s.error" class="notice error" role="alert">{{ s.error }}</p><p v-if="s.message" class="notice" role="status">{{ s.message }}</p>
    <p v-if="!s.teamId" class="empty">チームとプロジェクトを選ぶと、実際の変更一覧を表示します。</p>
    <p v-else-if="!s.tasks.length && !s.loading" class="muted">自分の担当タスクはありません。チームの変更は下の一覧から確認できます。</p>
    <div class="workspace-grid">
      <section class="card own-work" aria-labelledby="own-title">
        <h2 id="own-title">自分の作業</h2>
        <template v-if="task">
          <h3>{{ task.title }}</h3><p>タスク進捗：{{ taskProgress }} <span class="badge">{{ basisLabel }}</span></p>
          <dl><dt>作業開始日時</dt><dd>{{ date(s.basis?.started_at) }}</dd><dt>作業開始時のバージョン</dt><dd>{{ s.basis?.base_version ? s.basis.base_version.slice(0,12) : '未取得（管理側で固定）' }}</dd><dt>作業環境</dt><dd>{{ s.environment?.state === 'CONFIGURED' ? '管理側の接続設定あり（実機検証は別途必要）' : '準備中・接続できません' }}</dd><dt>保存状態</dt><dd>{{ s.sessionConnected ? '提出時に管理側で再確認します' : '保存状態を確認できない' }}</dd></dl>
          <div class="buttons"><button :disabled="s.busy || s.basis?.state === 'ACTIVE'" @click="model.prepareEnvironment(false)">作業環境を準備</button><button class="secondary" :disabled="s.busy" @click="model.refreshEnvironment">接続条件を確認</button><button class="secondary" :disabled="s.busy" @click="model.checkEnvironment">準備結果を確認</button><button class="secondary" :disabled="s.busy || !model.active()" @click="model.connectSession">編集セッションを確認</button><button class="secondary" :disabled="s.busy || !model.active() || !s.sessionConnected" @click="model.prepareEnvironment(true)">最新版を取得（新しい環境）</button></div>
          <div class="buttons"><button class="secondary" :disabled="s.busy" @click="model.refreshWork">作業環境・基準を確認</button></div>
          <button :disabled="s.busy || !s.sessionConnected" @click="model.openEditor">code-serverの接続を準備</button><a v-if="s.editorUrl" :href="s.editorUrl" target="_blank" rel="noopener noreferrer">code-serverを開く</a>
          <p class="notice">保存だけでは変更を提出しません。管理側の検証基盤が未接続なら操作は拒否されます。以前の作業環境は上書きしません。</p>
          <template v-if="s.preview?.diff"><h3>提出前のコード差分</h3><p v-if="s.preview.diff.truncated">表示上限により一部を省略しています。</p><details v-for="file in s.preview.diff.files.slice(0,40)" :key="file.path" class="file-diff"><summary>{{ file.path }}</summary><p v-if="file.binary">バイナリのため本文を表示しません。</p><pre v-else>{{ (file.patch || '').slice(0,20000) }}</pre></details></template>
          <button :disabled="!s.sessionConnected || !model.active() || s.busy || !!s.journal?.request_id" @click="model.collect">保存済みファイルを収集</button>
          <p v-if="!s.sessionConnected" class="muted">編集セッションと保存状態を検証できないため、収集・実提出を開始できません。</p>
          <p v-if="s.journal?.pending" class="notice warning">確認が必要：操作結果が不明です。別のIDで再提出しません。</p>
          <button class="secondary" :disabled="s.busy || !s.journal" @click="model.checkResult">提出結果を確認</button>
          <dl v-if="s.result"><dt>収集状態</dt><dd>{{ collectionLabel }}</dd><dt>変更提出の状態</dt><dd>{{ conversionLabel }}</dd><dt>レビュー依頼</dt><dd><button v-if="s.result.change_request_id" class="link-button" @click="model.openReview(s.result.change_request_id)">依頼 #{{ s.result.change_request_id }} を確認</button><span v-else>作成を確認していません</span></dd></dl>
          <button v-if="['READY','NO_CHANGE'].includes(s.result?.conversion_state)" class="secondary" :disabled="s.busy || !!s.journal?.pending" @click="model.nextChange">次の変更・修正後の提出を準備</button>
          <fieldset :disabled="s.busy || !!s.journal?.pending">
            <legend>変更を確認してから提出</legend>
            <label>変更タイトル<input v-model="s.title" maxlength="200" @input="model.invalidatePreview" placeholder="例：検索結果の表示を改善" /></label>
            <label>変更の説明<textarea v-model="s.description" maxlength="4000" rows="3" @input="model.invalidatePreview" /></label><p v-if="s.previousRequestId">修正前の依頼：#{{ s.previousRequestId }}</p>
            <button class="secondary" :disabled="!s.journal?.submission_id || !model.active()" @click="model.loadCandidates">削除内容を確認</button>
            <ul class="plain-list"><li v-for="c in s.candidates" :key="c.id"><label class="check"><input v-model="s.selectedDeletes" type="checkbox" :value="c.id" @change="model.invalidatePreview" />{{ c.path }}</label></li></ul>
            <button v-if="s.candidates.length" class="danger secondary" :disabled="!s.selectedDeletes.length" @click="model.confirmDeletions">選択した削除を明示確認</button>
            <p class="muted">削除を自動承認しません。登録済み証拠は変更確認で再照合されます。</p><p v-if="s.deletionIds.length" class="notice">削除意図を{{ s.deletionIds.length }}件登録済みです。取消APIは未提供です。誤選択の場合は提出せず管理者に確認してください。</p><button :disabled="!model.canPreview()" @click="model.preview">変更を確認</button>
          </fieldset>
          <section v-if="s.preview" class="preview" aria-label="提出前の確認内容">
            <h3>{{ s.preview.no_change ? '変更なし' : '変更確認済み' }}</h3><p>確認の有効期限：{{ date(s.preview.expires_at) }} <strong v-if="expired">期限切れ・再確認してください</strong></p>
            <p v-if="s.preview.main_differs" class="notice warning">チームの共有コードが作業開始後に更新されています。自動統合は行いません。</p>
            <ul><li v-for="f in s.preview.changes" :key="f.path"><span class="badge">{{ changeLabel(f.change) }}</span> {{ f.path }}</li></ul><h4>保持される除外対象</h4><ul><li v-for="p in s.preview.excluded_paths" :key="p">{{ p }}</li></ul>
            <p class="muted">基準内の除外対象を表示しています。現在の除外ファイル全件はAPI未提供です。除外規則の版：{{ s.preview.excluded_policy_version }}</p>
            <button :disabled="expired || !model.canSubmit()" @click="model.submit">{{ s.preview.no_change ? '変更なしを確定' : '変更を提出' }}</button><p class="muted">ここを押したときだけ変更提出を行います。共有コードへの反映は別操作です。</p>
          </section>
        </template><p v-else class="empty">自分の担当タスクを選んでください。</p>
      </section>
      <section class="card team-work" aria-labelledby="team-title">
        <div class="section-heading"><h2 id="team-title">チームの変更一覧</h2><button class="secondary" :disabled="!s.projectId || s.loading" @click="model.loadReviews()">一覧を再読込み</button></div>
        <label>表示する状態<select v-model="s.filter"><option v-for="f in ['すべて','レビュー待ち','承認済み','統合済み','修正が必要','確認が必要']" :key="f">{{ f }}</option></select></label>
        <p v-if="s.projectId && !s.loading && !filtered.length" class="empty">このページに該当する変更はありません。</p>
        <ul class="review-list"><li v-for="r in filtered" :key="r.id"><button class="review-card" @click="model.openReview(r.id)"><strong>{{ r.title }}</strong><span class="badge">{{ reviewLabel(r) }}</span><small>{{ r.submitter_name || '名前未取得' }} · {{ date(r.created_at) }}</small><small>関連タスク・ファイル数：一覧APIでは未提供</small></button></li></ul>
        <div class="buttons"><button class="secondary" :disabled="s.loading || !s.offset" @click="model.loadReviews(Math.max(0,s.offset-10))">前のページ</button><button class="secondary" :disabled="s.loading || !s.more" @click="model.loadReviews(s.offset+10)">次のページ</button></div><p class="muted">絞込みは現在のページに適用します。統合状態が取得できない変更は「確認が必要」です。</p>
      </section>
    </div>
    <section class="card detail" aria-label="レビュー詳細">
      <p v-if="s.detailLoading" role="status">変更内容を読み込んでいます…</p><p v-if="s.detailError" class="notice error" role="alert">{{ s.detailError }}</p>
      <template v-if="s.detail">
        <div class="section-heading"><h2>{{ s.detail.title }}</h2><span class="badge">{{ reviewLabel(s.detail) }}</span><button class="secondary" :disabled="s.detailLoading" @click="model.openReview(s.detail.id)">詳細・反映結果を確認</button></div><p>提出者：{{ s.detail.submitter_name || '名前未取得' }} · {{ date(s.detail.created_at) }}</p><p class="preserve">{{ s.detail.description || '説明はありません。' }}</p><p>関連タスク：{{ s.detail.task_context?.task_title || '対応記録なし・未取得' }} <span v-if="s.detail.task_context?.previous_request_id">／修正前の依頼 #{{ s.detail.task_context.previous_request_id }}</span></p>
        <p>レビュー：{{ {PENDING:'レビュー待ち',APPROVED:'承認済み',CHANGES_REQUESTED:'修正が必要'}[s.detail.review_decision] || '未取得' }} / 反映：{{ s.detail.integration?.status === 'COMPLETED' ? '統合済み' : s.detail.integration?.status === 'PREPARED' ? '確認が必要' : s.detail.integration === null ? '未反映' : '未取得' }}</p>
        <p v-if="s.detail.reviewer_name">確認したメンバー：{{ s.detail.reviewer_name }}</p><p v-if="s.detail.review_reason" class="preserve notice">修正依頼のコメント：{{ s.detail.review_reason }}</p><p v-if="s.reviewUncertain" class="notice warning">操作結果が不明です。新しい操作をせず、詳細・反映結果を確認してください。</p><p v-if="s.detail.submitted_by === s.me?.id" class="muted">自分の変更は承認・修正依頼できません。</p>
        <div class="buttons"><button :disabled="!permission.review" @click="model.reviewAction('approve')">承認する</button><button :disabled="!permission.integrate" @click="model.reviewAction('integrate')">チームに反映</button></div><label>修正を依頼する理由<textarea v-model="s.reason" maxlength="2000" rows="3" :disabled="!permission.review" /></label><button class="secondary" :disabled="!permission.review || !s.reason.trim()" @click="model.reviewAction('request-changes')">修正を依頼</button><p class="muted">所属・提出者・レビュー状態が確認できた場合だけ操作できます。最終認可と基準不一致の判定はサーバーで行います。</p>
        <template v-if="s.diff"><h3>変更内容 · {{ s.diff.files.length }} ファイル</h3><p v-if="s.diff.truncated" class="notice warning">一部の差分は上限により省略されています。</p>
          <!-- ファイル名・コード・コメントをHTMLとして解釈せず、常にテキスト描画する。 -->
          <details v-for="f in s.diff.files.slice(0,s.visibleFiles)" :key="f.path" class="file-diff"><summary>{{ changeLabel(f.change) }} · {{ f.path }} <span v-if="f.mode_changed">（ファイル属性の変更）</span></summary><p v-if="f.binary">バイナリのため本文を表示しません。</p><pre v-else>{{ (f.patch || '表示できる差分はありません。').slice(0,20000) }}</pre><p v-if="f.truncated || f.patch?.length > 20000">差分の一部を省略しています。</p></details><button v-if="s.diff.files.length > s.visibleFiles" class="secondary" @click="s.visibleFiles += 40">次の40ファイルを表示</button>
        </template>
      </template><p v-else-if="!s.detailLoading" class="empty">一覧から変更を選ぶと、レビュー内容を確認できます。</p>
    </section>
  </main>
</template>

<style scoped>
.sharing{max-width:1440px;margin:auto;padding:28px;color:#243449;background:#f3f6fb;min-height:100vh;font:16px/1.65 system-ui,sans-serif;box-sizing:border-box}.sharing *{box-sizing:border-box}.sharing h1{font-size:30px;letter-spacing:0;margin:0;color:#17345e}.sharing h2{font-size:21px;margin:0 0 16px;color:#17345e}.sharing h3{font-size:18px}.page-header,.section-heading{display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap}.eyebrow{color:#476e9e;font-size:13px;margin:0}.page-header nav,.buttons{display:flex;gap:12px;flex-wrap:wrap}.sharing a,.link-button{color:#2455a0}.sharing a:focus-visible,.sharing button:focus-visible,.sharing input:focus-visible,.sharing select:focus-visible,.sharing textarea:focus-visible{outline:3px solid #f0b831;outline-offset:2px}.card{background:white;border:1px solid #dbe3ef;border-radius:14px;padding:24px;margin-top:22px;box-shadow:0 3px 12px #1c396008}.context{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.context p{grid-column:1/-1;margin:0}.workspace-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:22px}.sharing label{display:block;font-weight:600;margin:10px 0}.sharing input,.sharing select,.sharing textarea{display:block;width:100%;font:inherit;color:#243449;background:white;border:1px solid #aebed2;border-radius:6px;padding:9px;margin-top:6px}.sharing button{font:inherit;font-weight:600;border:1px solid #2455a0;border-radius:7px;background:#2455a0;color:white;padding:9px 16px;cursor:pointer;margin:5px 0}.sharing button.secondary{background:white;color:#2455a0}.sharing button.danger{color:#9a3030;border-color:#9a3030}.sharing button:disabled{opacity:.5;cursor:not-allowed}.sharing fieldset{border:1px solid #dbe3ef;border-radius:8px;margin:22px 0;padding:16px}.sharing legend{font-weight:700}.notice{padding:12px 16px;border-radius:8px;background:#edf5ff;border-left:4px solid #3979be;overflow-wrap:anywhere}.notice.error{background:#fff0f0;border-color:#b84545}.notice.warning{background:#fff7df;border-color:#c3911d}.muted,small{color:#5a6c81;font-size:14px}.empty{padding:22px 0;color:#65778a}.badge{font-size:13px;padding:3px 10px;border-radius:20px;background:#edf2fb;color:#204f83;display:inline-block}.review-list,.plain-list{list-style:none;padding:0}.sharing .review-card{display:grid;grid-template-columns:1fr auto;gap:8px;text-align:left;width:100%;background:white;color:#243449;border:1px solid #dbe3ef;padding:16px}.review-card small{grid-column:1/-1}.sharing dl{display:grid;grid-template-columns:145px 1fr;gap:8px;overflow-wrap:anywhere}.sharing dt{color:#5a6c81}.sharing dd{margin:0}.sharing .check{display:flex;align-items:center;gap:8px;overflow-wrap:anywhere;font-weight:400}.sharing .check input{width:auto;margin:0}.file-diff{border:1px solid #dbe3ef;border-radius:6px;margin:12px 0;overflow:hidden}.file-diff summary{padding:12px;cursor:pointer;overflow-wrap:anywhere;background:#f5f7fa}.file-diff pre{font:13px/1.6 ui-monospace,Consolas,monospace;overflow:auto;max-height:420px;padding:18px;white-space:pre;color:#253d51;background:#f9fbfc;margin:0}.preserve{white-space:pre-wrap;overflow-wrap:anywhere}.sharing .link-button{border:0;background:transparent;padding:0;text-decoration:underline}@media(max-width:850px){.sharing{padding:16px}.workspace-grid,.context{grid-template-columns:1fr}.sharing dl{grid-template-columns:1fr}.context p{grid-column:auto}.card{padding:18px}.sharing h1{font-size:25px}}
</style>
