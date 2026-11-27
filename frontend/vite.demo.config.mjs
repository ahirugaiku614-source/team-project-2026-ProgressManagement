import {defineConfig} from 'vite'
import vue from '@vitejs/plugin-vue'
import {fileURLToPath} from 'node:url'

// 合成デモを通常製品の配布物へ混ぜない。生成先も既存のignored artifacts配下にする。
// 空にする処理は使わず、既存の検証用一時領域を削除しない。
export default defineConfig({plugins:[vue()],build:{
  outDir:'artifacts/stage9-demo',emptyOutDir:false,
  rolldownOptions:{input:fileURLToPath(new URL('./stage9-demo.html',import.meta.url))},
}})
