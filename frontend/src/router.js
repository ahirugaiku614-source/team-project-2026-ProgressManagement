
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

// 初回の認証確認が完了するまで待機するPromise
let resolveAuthReady

const authReady = new Promise((resolve) => {
  resolveAuthReady = resolve
})

// Firebaseのログイン状態を常に監視
onAuthStateChanged(auth, (user) => {
  currentUser = user

  // 初回の認証確認が完了したとき
  if (!authInitialized) {
    authInitialized = true
    resolveAuthReady()
  }
})

// 画面遷移前の認証チェック
router.beforeEach(async (to) => {

  // Firebaseの初回認証確認を待つ
  await authReady

  // 未ログインの場合、ログイン画面へ移動
  if (to.meta.requiresAuth && !currentUser) {
    return { name: 'Login' }
  }

  // ログイン済みの場合、ログイン画面からホームへ移動
  if (to.name === 'Login' && currentUser) {
    return { name: 'Home' }
  }

  // 問題がなければ画面遷移を許可
  return true
})

export default router
