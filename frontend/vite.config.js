import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    host: true, // コンテナ外（ブラウザ）からのアクセスを許可
    port: 5173,
    proxy: {
      // Vue.js から /api へのリクエストを FastAPI (http://backend:8000) へ転送する設定
      '/api': {
        target: 'http://backend:8000',
        changeOrigin: true,
      }
    }
  }
})
