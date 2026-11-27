
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

      <div class="mode-tabs">
        <button
          type="button"
          :class="{ active: mode === 'create' }"
          @click="changeMode('create')"
          :disabled="isLoading"
        >
          新しいチームを作成
        </button>

        <button
          type="button"
          :class="{ active: mode === 'join' }"
          @click="changeMode('join')"
          :disabled="isLoading"
        >
          既存のチームに参加
        </button>
      </div>

      <form @submit.prevent="handleSubmit">

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
            ></textarea>
          </div>
        </template>

        <!-- 既存チームに参加 -->
        <template v-else>
          <div class="form-group">
            <label for="invite-code">招待コード</label>
            <input
              id="invite-code"
              v-model.trim="inviteCode"
              type="text"
              placeholder="招待コードを入力してください"
              required
            />
          </div>
        </template>

        <div class="form-group">
          <label for="project-name">プロジェクト名</label>
          <input
            id="project-name"
            v-model.trim="projectName"
            type="text"
            maxlength="100"
            placeholder="例：進捗管理システム"
            required
          />
        </div>

        <div class="form-group">
          <label for="project-description">プロジェクトの説明</label>
          <textarea
            id="project-description"
            v-model.trim="projectDescription"
            placeholder="プロジェクトの内容を入力してください"
            rows="4"
          ></textarea>
        </div>

        <p v-if="errorMessage" class="error-message" role="alert">
          {{ errorMessage }}
        </p>

        <button
          type="submit"
          class="submit-button"
          :disabled="isLoading"
        >
          {{
            isLoading
              ? '登録処理中...'
              : mode === 'create'
                ? 'チーム・プロジェクトを作成'
                : '参加してプロジェクトを登録'
          }}
        </button>
      </form>

      <p class="note">
        ※ プロジェクトを登録するとホーム画面へ移動します。
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { auth } from '../firebase'
import { clearProjectStatusCache } from '../services/projectSetup'

const router = useRouter()
const route = useRoute()

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

const mode = ref('create')

const teamName = ref('')
const teamDescription = ref('')
const inviteCode = ref('')

const projectName = ref('')
const projectDescription = ref('')

const isLoading = ref(false)
const errorMessage = ref('')

const checkError = computed(() => route.query.checkError === '1')

// API通信
const apiRequest = async (url, options = {}) => {
  const user = auth.currentUser

  if (!user) {
    throw new Error('ログイン情報を確認できません。')
  }

  const token = await user.getIdToken()

  const response = await fetch(`${API_BASE_URL}${url}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers
    }
  })

  if (!response.ok) {
    let message = '登録処理に失敗しました。'

    try {
      const data = await response.json()
      message = data.detail || message
    } catch {
      // JSON形式でないエラーの場合
    }

    throw new Error(message)
  }

  return response.json()
}

// 表示モードの切り替え
const changeMode = (newMode) => {
  mode.value = newMode
  errorMessage.value = ''
}

// プロジェクト状況の再確認
const retryCheck = async () => {
  errorMessage.value = ''

  try {
    clearProjectStatusCache()

    const { checkUserHasProjects } = await import(
      '../services/projectSetup'
    )

    const hasProjects = await checkUserHasProjects(auth.currentUser)

    if (hasProjects) {
      await router.replace('/')
    } else {
      await router.replace('/project-register')
    }
  } catch (error) {
    console.error(error)
    errorMessage.value = error.message
  }
}

// 登録処理
const handleSubmit = async () => {
  if (isLoading.value) return

  errorMessage.value = ''

  if (!projectName.value.trim()) {
    errorMessage.value = 'プロジェクト名を入力してください。'
    return
  }

  if (mode.value === 'create' && !teamName.value.trim()) {
    errorMessage.value = 'チーム名を入力してください。'
    return
  }

  if (mode.value === 'join' && !inviteCode.value.trim()) {
    errorMessage.value = '招待コードを入力してください。'
    return
  }

  isLoading.value = true

  try {
    let team

    if (mode.value === 'create') {
      // 新しいチームを作成
      team = await apiRequest('/api/teams', {
        method: 'POST',
        body: JSON.stringify({
          name: teamName.value.trim(),
          description: teamDescription.value.trim() || null
        })
      })
    } else {
      // 招待コードで既存チームに参加
      team = await apiRequest('/api/teams/join', {
        method: 'POST',
        body: JSON.stringify({
          invite_code: inviteCode.value.trim()
        })
      })
    }

    // チームIDを使ってプロジェクトを登録
    await apiRequest(`/api/teams/${team.id}/projects`, {
      method: 'POST',
      body: JSON.stringify({
        name: projectName.value.trim(),
        description: projectDescription.value.trim() || null
      })
    })

    // プロジェクト登録状態のキャッシュをリセット
    clearProjectStatusCache()

    // ホーム画面へ移動
    await router.replace('/')
  } catch (error) {
    console.error('チーム・プロジェクト登録エラー:', error)

    errorMessage.value =
      error.message || '登録中にエラーが発生しました。'
  } finally {
    isLoading.value = false
  }
}
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
textarea {
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

input {
  min-height: 45px;
}

textarea {
  resize: vertical;
}

input:focus,
textarea:focus {
  outline: none;
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
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
