

import { createRouter, createWebHistory } from 'vue-router'
import { onAuthStateChanged } from 'firebase/auth'
import { auth } from './firebase'

// 各画面の読み込み
import Home from './components/Home.vue'
import Login from './components/Login.vue'
import schedule from './components/schedule.vue'
import Editor from './components/Editor.vue'
import CodeSharing from './components/CodeSharing.vue'

// ルート設定
const routes = [
  {
    path: '/',
    name: 'Home',
    component: Home,
    meta: { requiresAuth: true }
  },
  {
    path: '/login',
    name: 'Login',
    component: Login
  },
  {
    path: '/schedule',
    name: 'schedule',
    component: schedule,
    meta: { requiresAuth: true }
  },
  {
    path: '/Editor',
    name: 'Editor',
    component: Editor,
    meta: { requiresAuth: true }
  },
  {
    path: '/CodeSharing',
    name: 'CodeSharing',
    component: CodeSharing,
    meta: { requiresAuth: true }
  },
  {
    // 存在しないURLの場合はホーム画面へ
    path: '/:pathMatch(.*)*',
    redirect: '/'
  }
]

// Vue Routerの作成
const router = createRouter({
  history: createWebHistory(),
  routes
})

// Firebaseの認証状態
let currentUser = null
let authInitialized = false
let resolveAuthReady

// 初回認証確認を待機するPromise
const authReady = new Promise((resolve) => {
  resolveAuthReady = resolve
})

// ログイン・ログアウト状態を監視
onAuthStateChanged(auth, (user) => {
  currentUser = user

  if (!authInitialized) {
    authInitialized = true
    resolveAuthReady()
  }
})

// 画面遷移前の認証チェック
router.beforeEach(async (to) => {
  await authReady

  // 未ログインの場合
  if (to.meta.requiresAuth && !currentUser) {
    return { name: 'Login' }
  }

  // ログイン済みの場合
  if (to.name === 'Login' && currentUser) {
    return { name: 'Home' }
  }

  return true
})

export default router

