
<template>
  <div class="register-container">
    <div class="register-card">
      <h1>チーム・プロジェクト登録</h1>

      <p class="description">
        チームを作成するか、招待コードで既存のチームに参加してください。
      </p>

      <div v-if="checkError" class="error-message">
        プロジェクト情報を確認できませんでした。
        サーバーの状態を確認して、もう一度お試しください。

        <button
          type="button"
          class="retry-button"
          @click="retryCheck"
          :disabled="isLoading"
        >
          再確認する
        </button>
      </div>

      <p v-if="isChecking" class="note">
        プロジェクト登録状況を確認しています...
      </p>

      <div v-if="pendingOperation" class="success-message">
        前回の登録処理が残っています。
        同じ操作IDを使用して登録を再開します。
      </div>

      <div class="mode-tabs">
        <button
          type="button"
          :class="{ active: mode === 'create' }"
          @click="changeMode('create')"
          :disabled="blocked || !!pendingOperation || !!joinedTeam"
        >
          新しいチームを作成
        </button>

        <button
          type="button"
          :class="{ active: mode === 'join' }"
          @click="changeMode('join')"
          :disabled="blocked || !!pendingOperation || !!joinedTeam"
        >
          既存のチームに参加
        </button>
      </div>

      <form @submit.prevent="handleSubmit">
        <fieldset :disabled="blocked">

          <!-- 新しいチームを作成 -->
          <template v-if="mode === 'create'">
            <div class="form-group">
              <label for="team-name">チーム名</label>

              <input
                id="team-name"
                v-model.trim="teamName"
                type="text"
                maxlength="100"
                placeholder="例：開発チームA"
                :disabled="!!pendingOperation"
                required
              />
            </div>

            <div class="form-group">
              <label for="team-description">チームの説明</label>

              <textarea
                id="team-description"
                v-model.trim="teamDescription"
                placeholder="チームの目的などを入力してください"
                rows="3"
                :disabled="!!pendingOperation"
              ></textarea>
            </div>
          </template>

          <!-- 既存チームへの参加 -->
          <template v-else>
            <div v-if="!joinedTeam" class="form-group">
              <label for="invite-code">招待コード</label>

              <input
                id="invite-code"
                v-model.trim="inviteCode"
                type="text"
                placeholder="招待コードを入力してください"
                required
              />
            </div>

            <div
              v-if="!joinedTeam && availableTeams.length"
              class="form-group"
            >
              <label for="joined-team">
                または、所属済みのチームを選択
              </label>

              <select
                id="joined-team"
                v-model.number="selectedTeamId"
                @change="inviteCode = ''"
              >
                <option :value="null">
                  チームを選択してください
                </option>

                <option
                  v-for="team in availableTeams"
                  :key="team.id"
                  :value="team.id"
                >
                  {{ team.name }}
                </option>
              </select>
            </div>

            <template v-if="joinedTeam">
              <div class="success-message">
                チーム：{{ joinedTeam.name }}
                <p>チームへの参加が完了しています。</p>
              </div>

              <button
                v-if="!projectsLoaded"
                type="button"
                class="submit-button"
                @click="retryLoadProjects"
              >
                プロジェクト一覧を再取得
              </button>

              <template v-if="projectsLoaded">
                <div class="form-group">
                  <label>プロジェクトの登録方法</label>

                  <label
                    v-if="existingProjects.length"
                    class="radio-label"
                  >
                    <input
                      v-model="projectMode"
                      type="radio"
                      value="existing"
                      :disabled="!!pendingOperation"
                    />
                    既存のプロジェクトを利用する
                  </label>

                  <label class="radio-label">
                    <input
                      v-model="projectMode"
                      type="radio"
                      value="new"
                      :disabled="!!pendingOperation"
                    />
                    新しいプロジェクトを作成する
                  </label>
                </div>

                <div
                  v-if="projectMode === 'existing'"
                  class="form-group"
                >
                  <label for="existing-project">
                    プロジェクトを選択
                  </label>

                  <select
                    id="existing-project"
                    v-model.number="selectedProjectId"
                    required
                  >
                    <option :value="null">
                      プロジェクトを選択してください
                    </option>

                    <option
                      v-for="project in existingProjects"
                      :key="project.id"
                      :value="project.id"
                    >
                      {{ project.name }}
                    </option>
                  </select>
                </div>
              </template>
            </template>
          </template>

          <!-- 新規プロジェクトの入力欄 -->
          <template v-if="showProjectFields">
            <div class="form-group">
              <label for="project-name">
                プロジェクト名
              </label>

              <input
                id="project-name"
                v-model.trim="projectName"
                type="text"
                maxlength="100"
                placeholder="例：進捗管理システム"
                :disabled="!!pendingOperation"
                required
              />
            </div>

            <div class="form-group">
              <label for="project-description">
                プロジェクトの説明
              </label>

              <textarea
                id="project-description"
                v-model.trim="projectDescription"
                placeholder="プロジェクトの内容を入力してください"
                rows="4"
                :disabled="!!pendingOperation"
              ></textarea>
            </div>
          </template>

          <button
            type="submit"
            class="submit-button"
            :disabled="
              blocked ||
              (mode === 'join' && joinedTeam && !projectsLoaded)
            "
            :formnovalidate="mode === 'join' && !joinedTeam"
          >
            {{ submitLabel }}
          </button>

        </fieldset>
      </form>

      <p v-if="errorMessage" class="error-message" role="alert">
        {{ errorMessage }}
      </p>

      <p class="note">
        ※ プロジェクトを登録するとホーム画面へ移動します。
      </p>
    </div>
  </div>
</template>

<script setup>
import {
  ref,
  computed,
  onMounted,
  onUnmounted,
  watch
} from 'vue'

import { useRouter, useRoute } from 'vue-router'
import { onAuthStateChanged } from 'firebase/auth'
import { auth } from '../firebase'

import {
  API_BASE_URL,
  apiRequest
} from '../services/api'

import {
  checkUserHasProjects,
  clearProjectStatusCache,
  getUserTeams,
  getTeamProjects
} from '../services/projectSetup'

const router = useRouter()
const route = useRoute()

const mode = ref('create')

const teamName = ref('')
const teamDescription = ref('')
const inviteCode = ref('')

const projectName = ref('')
const projectDescription = ref('')

const joinedTeam = ref(null)
const availableTeams = ref([])
const selectedTeamId = ref(null)

const existingProjects = ref([])
const selectedProjectId = ref(null)
const projectMode = ref('new')
const projectsLoaded = ref(false)

const isLoading = ref(false)
const isChecking = ref(true)
const checkFailed = ref(false)
const registrationAllowed = ref(false)
const errorMessage = ref('')

const pendingOperation = ref(null)

let authVersion = 0
let disposed = false
let initialUid = auth.currentUser?.uid

const checkError = computed(() => {
  return checkFailed.value || route.query.checkError === '1'
})

const blocked = computed(() => {
  return (
    isLoading.value ||
    isChecking.value ||
    checkError.value ||
    !registrationAllowed.value
  )
})

const showProjectFields = computed(() => {
  return (
    mode.value === 'create' ||
    (
      joinedTeam.value &&
      projectsLoaded.value &&
      projectMode.value === 'new'
    )
  )
})

const submitLabel = computed(() => {
  if (isLoading.value) {
    return '登録処理中...'
  }

  if (pendingOperation.value) {
    return '登録処理を再開する'
  }

  if (mode.value === 'create') {
    return 'チーム・プロジェクトを作成'
  }

  if (!joinedTeam.value) {
    return 'チームに参加する'
  }

  if (projectMode.value === 'existing') {
    return '選択したプロジェクトを利用する'
  }

  return 'プロジェクトを作成する'
})

// 現在の認証状態を取得
function getSession() {
  const user = auth.currentUser

  if (!user) {
    throw new Error('ログイン情報を確認できません。')
  }

  return {
    user,
    version: authVersion
  }
}

// 処理中にログアウトやユーザー変更がないか確認
function assertSession(session) {
  if (
    disposed ||
    auth.currentUser?.uid !== session.user.uid ||
    authVersion !== session.version
  ) {
    throw new Error('ログイン状態が変更されました。')
  }
}

// API接続先とユーザーごとに操作情報を分離
function storageKey(user) {
  return [
    'team-project-registration-v1',
    API_BASE_URL || window.location.origin,
    user.uid
  ].join(':')
}

// UUID形式の操作IDを生成
function createRequestKey() {
  const bytes = crypto.getRandomValues(new Uint8Array(16))

  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128

  const hex = Array.from(
    bytes,
    byte => byte.toString(16).padStart(2, '0')
  ).join('')

  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20)
  ].join('-')
}

// 操作情報を通信前に保存
function saveOperation(operation, session) {
  assertSession(session)

  localStorage.setItem(
    storageKey(session.user),
    JSON.stringify(operation)
  )

  pendingOperation.value = operation
}

// 登録完了時に操作情報を削除
function removeOperation(session) {
  assertSession(session)

  localStorage.removeItem(storageKey(session.user))
  pendingOperation.value = null
}

// 保存済みの操作情報を取得
function readOperation(session) {
  assertSession(session)

  const raw = localStorage.getItem(storageKey(session.user))

  if (!raw) {
    return null
  }

  let operation

  try {
    operation = JSON.parse(raw)
  } catch {
    throw new Error('保存済みの登録情報が不正です。')
  }

  const uuidPattern =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

  if (
    !operation ||
    !['create', 'join'].includes(operation.mode) ||
    !uuidPattern.test(operation.projectKey || '') ||
    typeof operation.projectName !== 'string' ||
    !operation.projectName.trim() ||
    typeof operation.projectDescription !== 'string'
  ) {
    throw new Error('保存済みの登録情報が不正です。')
  }

  if (
    operation.mode === 'create' &&
    (
      !uuidPattern.test(operation.teamKey || '') ||
      typeof operation.teamName !== 'string' ||
      !operation.teamName.trim() ||
      typeof operation.teamDescription !== 'string'
    )
  ) {
    throw new Error('保存済みのチーム情報が不正です。')
  }

  if (
    operation.teamId !== null &&
    (
      !Number.isSafeInteger(operation.teamId) ||
      operation.teamId <= 0
    )
  ) {
    throw new Error('保存済みのチームIDが不正です。')
  }

  if (operation.mode === 'join' && !operation.teamId) {
    throw new Error('参加済みチームの情報が不正です。')
  }

  return operation
}

// APIから返されたIDを確認
function validateEntity(entity) {
  if (
    !entity ||
    !Number.isSafeInteger(entity.id) ||
    entity.id <= 0
  ) {
    throw new Error('サーバーからの登録結果が不正です。')
  }

  return entity
}

// 表示モードの切り替え
function changeMode(newMode) {
  if (blocked.value || pendingOperation.value || joinedTeam.value) {
    return
  }

  mode.value = newMode
  errorMessage.value = ''
}

// チーム内のプロジェクトを取得
async function loadProjects(session) {
  assertSession(session)

  projectsLoaded.value = false
  existingProjects.value = []
  selectedProjectId.value = null

  const projects = await getTeamProjects(
    joinedTeam.value.id,
    session.user
  )

  assertSession(session)

  existingProjects.value = projects

  if (pendingOperation.value) {
    projectMode.value = 'new'
  } else {
    projectMode.value = projects.length ? 'existing' : 'new'
  }

  if (projects.length) {
    selectedProjectId.value = projects[0].id
  }

  projectsLoaded.value = true
}

// プロジェクト一覧の再取得
async function retryLoadProjects() {
  if (blocked.value || !joinedTeam.value) {
    return
  }

  isLoading.value = true
  errorMessage.value = ''

  try {
    await loadProjects(getSession())
  } catch (error) {
    errorMessage.value = error.message
  } finally {
    isLoading.value = false
  }
}

// 保存済みの登録処理を復元
async function restoreOperation(session) {
  const operation = readOperation(session)

  if (!operation) {
    return
  }

  pendingOperation.value = operation
  mode.value = operation.mode

  teamName.value = operation.teamName || ''
  teamDescription.value = operation.teamDescription || ''

  projectName.value = operation.projectName
  projectDescription.value = operation.projectDescription

  if (operation.teamId) {
    const team = availableTeams.value.find(
      item => item.id === operation.teamId
    )

    if (!team) {
      throw new Error(
        '前回の登録先チームへの所属を確認できません。'
      )
    }

    joinedTeam.value = team

    if (operation.mode === 'join') {
      await loadProjects(session)
    }
  }
}

// 初回確認・通信エラーからの再確認
async function retryCheck() {
  if (isLoading.value) {
    return
  }

  isLoading.value = true
  isChecking.value = true
  registrationAllowed.value = false
  checkFailed.value = false
  errorMessage.value = ''

  try {
    const session = getSession()

    clearProjectStatusCache()

    const hasProjects = await checkUserHasProjects(session.user)

    assertSession(session)

    if (hasProjects) {
      await router.replace('/')
      return
    }

    availableTeams.value = await getUserTeams(session.user)

    assertSession(session)

    await restoreOperation(session)

    assertSession(session)

    if (route.query.checkError === '1') {
      await router.replace({ name: 'ProjectRegister' })
    }

    assertSession(session)

    registrationAllowed.value = true

  } catch (error) {
    checkFailed.value = true
    errorMessage.value =
      error.message || '登録状況を確認できませんでした。'

  } finally {
    isLoading.value = false
    isChecking.value = false
  }
}

// 既存チームに参加
async function joinExistingTeam(session) {
  let team

  if (selectedTeamId.value && !inviteCode.value.trim()) {
    const teams = await getUserTeams(session.user)

    assertSession(session)

    team = teams.find(
      item => item.id === selectedTeamId.value
    )

    if (!team) {
      throw new Error('指定したチームへの所属を確認できません。')
    }

  } else {
    if (!inviteCode.value.trim()) {
      throw new Error('招待コードを入力してください。')
    }

    team = await apiRequest(
      '/api/teams/join',
      {
        method: 'POST',
        body: JSON.stringify({
          invite_code: inviteCode.value.trim()
        })
      },
      session.user
    )
  }

  assertSession(session)

  joinedTeam.value = validateEntity(team)

  clearProjectStatusCache()

  await loadProjects(session)
}

// 登録完了処理
async function finishRegistration(project, session) {
  assertSession(session)

  validateEntity(project)

  // 選択したプロジェクトの情報をユーザー別に保存
  localStorage.setItem(
    `${storageKey(session.user)}:selected-project`,
    JSON.stringify({
      team_id: joinedTeam.value.id,
      project_id: project.id
    })
  )

  removeOperation(session)
  clearProjectStatusCache()

  await router.replace('/')
}

// 新しいチーム・プロジェクトを作成
async function createProject(session) {
  let operation = pendingOperation.value

  if (!operation) {
    if (!projectName.value.trim()) {
      throw new Error('プロジェクト名を入力してください。')
    }

    if (mode.value === 'create' && !teamName.value.trim()) {
      throw new Error('チーム名を入力してください。')
    }

    operation = {
      mode: mode.value,
      teamKey: createRequestKey(),
      projectKey: createRequestKey(),

      teamId: joinedTeam.value?.id || null,

      teamName: teamName.value.trim(),
      teamDescription: teamDescription.value.trim(),

      projectName: projectName.value.trim(),
      projectDescription: projectDescription.value.trim()
    }

    // API通信より先に保存する
    saveOperation(operation, session)
  }

  assertSession(session)

  // チームがまだ確定していない場合
  if (operation.mode === 'create' && !operation.teamId) {
    const team = await apiRequest(
      '/api/teams',
      {
        method: 'POST',
        body: JSON.stringify({
          name: operation.teamName,
          description: operation.teamDescription || null,
          request_key: operation.teamKey
        })
      },
      session.user
    )

    assertSession(session)

    joinedTeam.value = validateEntity(team)

    operation = {
      ...operation,
      teamId: team.id
    }

    saveOperation(operation, session)
  }

  // 所属状況を再確認する
  const teams = await getUserTeams(session.user)

  assertSession(session)

  const team = teams.find(
    item => item.id === operation.teamId
  )

  if (!team) {
    throw new Error('登録先チームへの所属を確認できません。')
  }

  joinedTeam.value = team

  // 保存済みの操作IDでプロジェクトを作成
  const project = await apiRequest(
    `/api/teams/${operation.teamId}/projects`,
    {
      method: 'POST',
      body: JSON.stringify({
        name: operation.projectName,
        description: operation.projectDescription || null,
        request_key: operation.projectKey
      })
    },
    session.user
  )

  assertSession(session)

  await finishRegistration(project, session)
}

// 既存プロジェクトを利用
async function useExistingProject(session) {
  const projects = await getTeamProjects(
    joinedTeam.value.id,
    session.user
  )

  assertSession(session)

  const project = projects.find(
    item => item.id === selectedProjectId.value
  )

  if (!project) {
    throw new Error(
      '選択したプロジェクトを利用できません。'
    )
  }

  await finishRegistration(project, session)
}

// 登録ボタンの処理
async function handleSubmit() {
  if (blocked.value) {
    return
  }

  isLoading.value = true
  errorMessage.value = ''

  try {
    const session = getSession()

    if (pendingOperation.value) {
      await createProject(session)
      return
    }

    if (mode.value === 'join' && !joinedTeam.value) {
      await joinExistingTeam(session)
      return
    }

    if (mode.value === 'join') {
      if (!projectsLoaded.value) {
        throw new Error(
          'プロジェクト一覧を取得してください。'
        )
      }

      if (projectMode.value === 'existing') {
        await useExistingProject(session)
        return
      }
    }

    await createProject(session)

  } catch (error) {
    errorMessage.value =
      error.message || '登録中にエラーが発生しました。'

    if (error.status === 401 || error.status === 403) {
      registrationAllowed.value = false
      checkFailed.value = true
    }

  } finally {
    isLoading.value = false
  }
}

// 認証状態の変更を監視
const unsubscribe = onAuthStateChanged(auth, user => {
  if (user?.uid === initialUid) {
    return
  }

  initialUid = user?.uid
  authVersion += 1

  registrationAllowed.value = false
  clearProjectStatusCache()

  pendingOperation.value = null
  joinedTeam.value = null
  availableTeams.value = []
  existingProjects.value = []
  selectedProjectId.value = null
  selectedTeamId.value = null

  teamName.value = ''
  teamDescription.value = ''
  inviteCode.value = ''
  projectName.value = ''
  projectDescription.value = ''

  router.replace(user ? '/' : '/login')
})

// URLがエラー状態になった場合は登録を禁止
watch(
  () => route.query.checkError,
  value => {
    if (value === '1') {
      registrationAllowed.value = false
    }
  }
)

onMounted(() => {
  retryCheck()
})

onUnmounted(() => {
  disposed = true
  authVersion += 1
  unsubscribe()
})
</script>

<style scoped>
.register-container {
  min-height: 100vh;
  box-sizing: border-box;
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 30px 16px;
  background: #f1f5f9;
}

.register-card {
  width: min(100%, 540px);
  box-sizing: border-box;
  padding: 32px;
  border-radius: 14px;
  background: #ffffff;
  box-shadow: 0 12px 35px rgba(15, 23, 42, 0.1);
}

h1 {
  margin: 0;
  text-align: center;
  color: #1e293b;
  font-size: 25px;
}

.description {
  margin: 14px 0 24px;
  color: #64748b;
  font-size: 14px;
  line-height: 1.7;
  text-align: center;
}

.mode-tabs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 26px;
}

.mode-tabs button {
  min-height: 48px;
  padding: 10px;
  border: 1px solid #cbd5e1;
  border-radius: 7px;
  background: #f8fafc;
  color: #475569;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.mode-tabs button.active {
  border-color: #2563eb;
  background: #eff6ff;
  color: #2563eb;
}

.mode-tabs button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

fieldset {
  border: none;
  padding: 0;
  margin: 0;
  min-width: 0;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 19px;
}

label {
  color: #334155;
  font-size: 14px;
  font-weight: 600;
}

input,
textarea,
select {
  width: 100%;
  box-sizing: border-box;
  padding: 12px;
  border: 1px solid #cbd5e1;
  border-radius: 7px;
  background: #ffffff;
  color: #1e293b;
  font-family: inherit;
  font-size: 14px;
}

input,
select {
  min-height: 45px;
}

textarea {
  resize: vertical;
}

input:focus,
textarea:focus,
select:focus {
  outline: none;
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
}

input:disabled,
textarea:disabled,
select:disabled {
  background: #f1f5f9;
  cursor: not-allowed;
}

.radio-label {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  cursor: pointer;
}

.radio-label input {
  width: 16px;
  min-height: 16px;
  height: 16px;
  margin: 0;
  accent-color: #2563eb;
}

.submit-button {
  width: 100%;
  min-height: 48px;
  margin-top: 8px;
  border: none;
  border-radius: 7px;
  background: #2563eb;
  color: #ffffff;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
}

.submit-button:hover:not(:disabled) {
  background: #1d4ed8;
}

.submit-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.error-message {
  margin: 12px 0;
  padding: 12px;
  border-radius: 7px;
  background: #fef2f2;
  color: #dc2626;
  font-size: 13px;
  line-height: 1.6;
}

.success-message {
  margin: 12px 0 20px;
  padding: 12px;
  border-radius: 7px;
  background: #f0fdf4;
  color: #166534;
  font-size: 14px;
  line-height: 1.6;
}

.success-message p {
  margin: 4px 0 0;
  font-size: 12px;
}

.retry-button {
  display: block;
  margin-top: 8px;
  padding: 6px 10px;
  border: 1px solid #dc2626;
  border-radius: 5px;
  background: #ffffff;
  color: #dc2626;
  cursor: pointer;
}

.retry-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.note {
  margin: 18px 0 0;
  color: #64748b;
  font-size: 12px;
  text-align: center;
}

@media (max-width: 480px) {
  .register-card {
    padding: 22px 18px;
  }

  h1 {
    font-size: 21px;
  }

  .mode-tabs button {
    font-size: 12px;
  }
}
</style>
