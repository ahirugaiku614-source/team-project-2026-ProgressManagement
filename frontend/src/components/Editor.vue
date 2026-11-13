
<template>
  <div class="editor-page">
    <!-- ヘッダー -->
    <header class="editor-header">
      <h2>Code Editor</h2>

      <div class="header-actions">
        <span class="current-file">
          {{ activeFile?.name }}
        </span>

        <button @click="deleteFile" :disabled="files.length <= 1">
          ファイル削除
        </button>
      </div>
    </header>

    <div class="editor-main">
      <!-- 左側：ファイル一覧 -->
      <aside class="file-sidebar">
        <h3>ファイル一覧</h3>

        <div class="file-create">
          <input
            v-model="newFileName"
            placeholder="例：sample.py"
            @keyup.enter="addFile"
          />
          <button @click="addFile">+</button>
        </div>

        <ul class="file-list">
          <li
            v-for="file in files"
            :key="file.id"
            :class="{ active: file.id === activeFileId }"
            @click="activeFileId = file.id"
          >
            {{ file.name }}
          </li>
        </ul>
      </aside>

      <!-- 右側：コードエディタ -->
      <section class="editor-workspace">
        <div class="editor-toolbar">
          <span>{{ activeFile?.name }}</span>

          <select
            v-if="activeFile"
            v-model="activeFile.language"
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

        <div class="editor-container" v-if="activeFile">
          <VueMonacoEditor
            :key="activeFile.id"
            v-model:value="editorCode"
            :language="activeFile.language"
            theme="vs-dark"
            :options="editorOptions"
          />
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { VueMonacoEditor } from '@guolao/vue-monaco-editor'

// ファイル一覧
const files = ref([
  {
    id: 1,
    name: 'main.py',
    language: 'python',
    content: 'print("Hello, World!")\n\nfor i in range(5):\n    print(i)'
  },
  {
    id: 2,
    name: 'sample.js',
    language: 'javascript',
    content: 'console.log("Hello, World!");'
  }
])

// 選択中のファイル
const activeFileId = ref(1)

// 新規ファイル名
const newFileName = ref('')

// ファイルID管理
let nextFileId = 3

// 選択中のファイルを取得
const activeFile = computed(() => {
  return files.value.find(
    file => file.id === activeFileId.value
  )
})

// Monaco Editorのコードとファイル内容を連動
const editorCode = computed({
  get() {
    return activeFile.value?.content ?? ''
  },
  set(value) {
    if (activeFile.value) {
      activeFile.value.content = value
    }
  }
})

// 拡張子から言語を判定
const getLanguage = (fileName) => {
  const extension = fileName.split('.').pop().toLowerCase()

  const languageMap = {
    py: 'python',
    js: 'javascript',
    html: 'html',
    css: 'css',
    ts: 'typescript',
    json: 'json'
  }

  return languageMap[extension] || 'plaintext'
}

// ファイル追加
const addFile = () => {
  let name = newFileName.value.trim()

  if (!name) {
    alert('ファイル名を入力してください')
    return
  }

  // 拡張子がない場合は.pyを付ける
  if (!name.includes('.')) {
    name += '.py'
  }

  // 同じ名前のファイルがあるか確認
  const exists = files.value.some(
    file => file.name === name
  )

  if (exists) {
    alert('同じ名前のファイルが存在します')
    return
  }

  const newFile = {
    id: nextFileId++,
    name,
    language: getLanguage(name),
    content: ''
  }

  files.value.push(newFile)
  activeFileId.value = newFile.id
  newFileName.value = ''
}

// ファイル削除
const deleteFile = () => {
  if (files.value.length <= 1) {
    alert('最後のファイルは削除できません')
    return
  }

  if (!confirm(`${activeFile.value.name} を削除しますか？`)) {
    return
  }

  const index = files.value.findIndex(
    file => file.id === activeFileId.value
  )

  files.value.splice(index, 1)

  // 削除後は先頭のファイルを選択
  activeFileId.value = files.value[0].id
}

// エディタ設定
const editorOptions = {
  automaticLayout: true,
  fontSize: 14,
  minimap: {
    enabled: true
  },
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
</style>

