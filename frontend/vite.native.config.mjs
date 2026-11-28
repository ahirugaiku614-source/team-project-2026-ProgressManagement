import {defineConfig} from 'vite'
import vue from '@vitejs/plugin-vue'
import {fileURLToPath} from 'node:url'

// Firebaseもmemory APIも含めない独立build。通常distへ混ぜず、既存成果物を削除しない。
export default defineConfig({plugins:[vue()],build:{outDir:'artifacts/stage10d-native',emptyOutDir:false,
  rolldownOptions:{input:fileURLToPath(new URL('./stage10d-native.html',import.meta.url))}}})
