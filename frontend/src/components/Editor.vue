
<template>
  <div class="editor-page">
    <header class="editor-header">
      <h2>Code Editor</h2>
      <div class="header-actions">
        <span class="current-file">
          {{ activeFile?.name ?? 'ファイル未選択' }}
        </span>
        <button
          @click="saveFile"
          :disabled="busy || !activeFile || !isDirty(activeFile)"
        >
          {{ busy ? '処理中...' : '保存' }}
        </button>
        <button
          @click="deleteFile"
          :disabled="busy || !activeFile || files.length <= 1"
        >
          ファイル削除
        </button>
      </div>
    </header>

    <div class="editor-main">
      <aside class="file-sidebar">
        <h3>ファイル一覧</h3>

        <div class="file-create">
          <input
            v-model="newFileName"
            placeholder="例：sample.py"
            :disabled="busy"
            @keyup.enter="addFile"
          />
          <button @click="addFile" :disabled="busy">
            +
          </button>
        </div>

        <ul class="file-list">
          <li
            v-for="file in files"
            :key="file.id"
            :class="{
              active: file.id === activeFileId,
              unsaved: isDirty(file)
            }"
            @click="activeFileId = file.id"
          >
            {{ file.name }}
          </li>
        </ul>

        <p
          v-if="!files.length && !busy"
          class="status-message"
        >
          ファイルがありません。新規作成してください。
        </p>
      </aside>

      <section class="editor-workspace">
        <div class="editor-toolbar">
          <span>
            {{ activeFile?.name ?? 'ファイルなし' }}
          </span>

          <select
            v-if="activeFile"
            v-model="activeFile.language"
            :disabled="busy"
          >
            <option value="python">Python</option>
            <option value="javascript">JavaScript</option>
            <option value="html">HTML</option>
            <option value="css">CSS</option>
            <option value="typescript">TypeScript</option>
            <option value="json">JSON</option>
            <option value="plaintext">Plain Text</option>
          </select>
        </div>

        <p
          v-if="message"
          class="status-message"
          :class="{ error: hasError }"
          role="status"
        >
          {{ message }}
        </p>

        <div v-if="activeFile" class="editor-container">
          <VueMonacoEditor
            :key="activeFile.id"
            v-model:value="editorCode"
            :language="activeFile.language"
            theme="vs-dark"
            :options="editorOptions"
          />
        </div>

        <p v-else class="empty-state">
          {{
            busy
              ? '読み込み中...'
              : '左側からファイルを作成してください。'
          }}
        </p>
      </section>
    </div>
  </div>
</template>

<script setup>
import {
  ref,
  computed,
  onMounted,
  onBeforeUnmount
} from 'vue'

import { VueMonacoEditor } from '@guolao/vue-monaco-editor'

// 共通API通信処理を使用
import { apiRequest } from '../services/api'

const files = ref([])
const activeFileId = ref(null)
const newFileName = ref('')
const busy = ref(false)
const message = ref('')
const hasError = ref(false)

const activeFile = computed(() =>
  files.value.find(
    f => f.id === activeFileId.value
  ) ?? null
)

const editorCode = computed({
  get: () => activeFile.value?.content ?? '',
  set: value => {
    if (activeFile.value) {
      activeFile.value.content = value
    }
  }
})

// サーバーから最後に保存した内容と比較する
const saved = new Map()

const snapshot = f =>
  JSON.stringify([f.name, f.language, f.content])

const isDirty = f =>
  f && snapshot(f) !== saved.get(f.id)

const anyDirty = () =>
  files.value.some(isDirty)

function showMessage(text, error = false) {
  message.value = text
  hasError.value = error
}

// ファイルの拡張子から言語を判定
const getLanguage = fileName => ({
  py: 'python',
  js: 'javascript',
  html: 'html',
  css: 'css',
  ts: 'typescript',
  json: 'json'
}[fileName.split('.').pop().toLowerCase()] || 'plaintext')

// ファイル一覧を取得
async function loadFiles() {
  busy.value = true
  showMessage('ファイルを読み込み中...')

  try {
    const data = await apiRequest('/api/files')

    if (!Array.isArray(data)) {
      throw new Error('ファイル一覧の応答データが不正です。')
    }

    files.value = data

    saved.clear()

    data.forEach(f => {
      saved.set(f.id, snapshot(f))
    })

    activeFileId.value = data[0]?.id ?? null

    showMessage('ファイルを読み込みました')

  } catch (error) {
    showMessage(error.message, true)

  } finally {
    busy.value = false
  }
}

// ファイルを新規作成
async function addFile() {
  if (busy.value) return

  let name = newFileName.value.trim()

  if (!name) {
    return showMessage(
      'ファイル名を入力してください',
      true
    )
  }

  if (!name.includes('.')) {
    name += '.py'
  }

  if (files.value.some(f => f.name === name)) {
    return showMessage(
      '同じ名前のファイルが存在します',
      true
    )
  }

  busy.value = true

  try {
    const file = await apiRequest('/api/files', {
      method: 'POST',
      body: JSON.stringify({
        name,
        language: getLanguage(name),
        content: ''
      })
    })

    files.value.push(file)

    saved.set(file.id, snapshot(file))

    activeFileId.value = file.id
    newFileName.value = ''

    showMessage(`${name} を作成しました`)

  } catch (error) {
    showMessage(error.message, true)

  } finally {
    busy.value = false
  }
}

// ファイルを保存
async function saveFile() {
  if (busy.value || !activeFile.value) return

  const file = activeFile.value

  // 通信中に別ファイルへ移動しても
  // 保存対象を取り違えないようにする
  const payload = {
    name: file.name,
    language: file.language,
    content: file.content
  }

  busy.value = true

  try {
    await apiRequest(`/api/files/${file.id}`, {
      method: 'PUT',
      body: JSON.stringify(payload)
    })

    saved.set(
      file.id,
      JSON.stringify([
        payload.name,
        payload.language,
        payload.content
      ])
    )

    showMessage(`${file.name} を保存しました`)

  } catch (error) {
    showMessage(error.message, true)

  } finally {
    busy.value = false
  }
}

// ファイルを削除
async function deleteFile() {
  if (
    busy.value ||
    !activeFile.value ||
    files.value.length <= 1
  ) {
    return
  }

  const file = activeFile.value

  if (!confirm(
    `${file.name} をデータベースから削除しますか？`
  )) {
    return
  }

  busy.value = true

  try {
    await apiRequest(`/api/files/${file.id}`, {
      method: 'DELETE'
    })

    files.value = files.value.filter(
      f => f.id !== file.id
    )

    saved.delete(file.id)

    activeFileId.value = files.value[0]?.id ?? null

    showMessage(`${file.name} を削除しました`)

  } catch (error) {
    showMessage(error.message, true)

  } finally {
    busy.value = false
  }
}

// 未保存ファイルがある場合に警告
function warnBeforeUnload(event) {
  if (!anyDirty()) return

  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => {
  window.addEventListener(
    'beforeunload',
    warnBeforeUnload
  )

  loadFiles()
})

onBeforeUnmount(() => {
  window.removeEventListener(
    'beforeunload',
    warnBeforeUnload
  )
})

// Monaco Editorの設定
const editorOptions = {
  automaticLayout: true,
  fontSize: 14,
  minimap: { enabled: true },
  scrollBeyondLastLine: false,
  wordWrap: 'on'
}
</script>

<style scoped>
.editor-page {
  height: 100vh;
  width: 100%;
  display: flex;
  flex-direction: column;
  background: #1e1e1e;
  color: white;
}

.editor-header {
  min-height: 55px;
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #252526;
}

.editor-header h2 {
  font-size: 18px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 15px;
}

button {
  padding: 7px 12px;
  color: white;
  background: #3c3c3c;
  border: 1px solid #555;
  border-radius: 4px;
  cursor: pointer;
}

button:hover {
  background: #505050;
}

button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.editor-main {
  flex: 1;
  min-height: 0;
  display: flex;
}

.file-sidebar {
  width: 230px;
  flex-shrink: 0;
  padding: 15px;
  background: #252526;
  border-right: 1px solid #444;
}

.file-sidebar h3 {
  font-size: 15px;
  margin-bottom: 15px;
}

.file-create {
  display: flex;
  gap: 5px;
  margin-bottom: 15px;
}

.file-create input {
  width: 100%;
  min-width: 0;
  padding: 7px;
  color: white;
  background: #333;
  border: 1px solid #555;
}

.file-list {
  list-style: none;
  padding: 0;
  margin: 0;
}

.file-list li {
  padding: 9px;
  cursor: pointer;
  border-radius: 4px;
  font-size: 14px;
}

.file-list li:hover {
  background: #383838;
}

.file-list li.active {
  background: #094771;
}

.editor-workspace {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.editor-toolbar {
  height: 40px;
  padding: 0 15px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #2d2d2d;
}

.editor-toolbar select {
  padding: 5px 10px;
  color: white;
  background: #333;
  border: 1px solid #555;
}

.editor-container {
  flex: 1;
  min-height: 0;
}

.file-list li.unsaved::after {
  content: ' ●';
  color: #e4b65a;
}

.status-message {
  font-size: 12px;
  color: #b9c8d8;
}

.status-message.error {
  color: #ff9b9b;
}

.empty-state {
  padding: 20px;
  color: #aaa;
}
</style>
