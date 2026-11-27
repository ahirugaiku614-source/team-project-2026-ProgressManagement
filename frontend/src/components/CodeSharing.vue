<script setup>
import { reactive, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { onAuthStateChanged } from 'firebase/auth'
import { auth } from '../firebase'
import { createApi } from '../code-sharing/api.mjs'
import { createWorkflow, initialState, positiveId } from '../code-sharing/workflow.mjs'
import CodeSharingPanel from './CodeSharingPanel.vue'

const route = useRoute(), router = useRouter(), state = reactive(initialState())
const request = createApi({ base: import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000',
  getToken: async () => auth.currentUser ? auth.currentUser.getIdToken() : null })
let storage = null
try { storage = window.sessionStorage } catch { /* 認証情報を別の場所へ保存する代替処理は作らない。 */ }
const model = createWorkflow({ request, state, storage, confirm: message => window.confirm(message) })
let routeRevision = 0, unsubscribe
async function applyRoute() {
  const revision = ++routeRevision
  if (!state.me) return
  const team = positiveId(route.query.team), project = positiveId(route.query.project), task = positiveId(route.query.task)
  if (team !== state.teamId) await model.selectTeam(team)
  if (revision !== routeRevision) return
  if (project !== state.projectId) await model.selectProject(project)
  if (revision !== routeRevision) return
  if (task !== state.taskId) await model.selectTask(task)
}
function choose(kind, value) {
  // コンテキストIDだけをURLへ保持する。確認ID・本文・許可値はURLへ入れない。
  const query = kind === 'team' ? { team: value } : kind === 'project'
    ? { team: state.teamId, project: value } : { team: state.teamId, project: state.projectId, task: value }
  router.replace({ name: 'CodeSharing', query: Object.fromEntries(Object.entries(query).filter(([, v]) => positiveId(v))) })
}
watch(() => [route.query.team, route.query.project, route.query.task], applyRoute)
onMounted(() => {
  unsubscribe = onAuthStateChanged(auth, async user => {
    routeRevision++; model.dispose()
    if (!user) { await router.replace({ name: 'Login' }); return }
    await model.bootstrap(); await applyRoute()
  })
})
onBeforeUnmount(() => { routeRevision++; unsubscribe?.(); model.dispose() })
</script>

<template><CodeSharingPanel :state="state" :model="model" @choose="choose" /></template>
