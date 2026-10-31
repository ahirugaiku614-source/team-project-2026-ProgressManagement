
<template>
  <div class="editor-page">
    <header class="editor-header">
      <h2>Code Editor</h2>

      <select v-model="language">
        <option value="python">Python</option>
        <option value="javascript">JavaScript</option>
        <option value="html">HTML</option>
        <option value="css">CSS</option>
      </select>
    </header>

    <div class="editor-container">
      <VueMonacoEditor
        v-model:value="code"
        :language="language"
        theme="vs-dark"
        :options="editorOptions"
      />
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { VueMonacoEditor } from '@guolao/vue-monaco-editor'

const language = ref('python')

const code = ref(
`print("Hello, World!")

for i in range(5):
    print(i)`
)

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
  display: flex;
  flex-direction: column;
  background: #1e1e1e;
}

.editor-header {
  height: 55px;
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #252526;
  color: white;
}

.editor-header h2 {
  font-size: 18px;
}

.editor-header select {
  padding: 6px 12px;
  background: #333;
  color: white;
  border: 1px solid #555;
  border-radius: 4px;
}

.editor-container {
  flex: 1;
  min-height: 0;
}
</style>


