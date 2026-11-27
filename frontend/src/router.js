
import { createRouter, createWebHistory } from 'vue-router'
import { onAuthStateChanged } from 'firebase/auth'
import { auth } from './firebase'

// 各画面の読み込み
import Home from './components/Home.vue'
import Login from './components/Login.vue'
import schedule from './components/schedule.vue'
import Editor from './components/Editor.vue'
import CodeSharing from './components/CodeSharing.vue'
import ProjectRegister from './components/ProjectRegister.vue'

// プロジェクト確認機能
import { checkUserHasProjects } from './services/projectSetup'

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
    path: '/project-register',
    name: 'ProjectRegister',
    component: ProjectRegister,
    meta: { requiresAuth: true }
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

// 画面遷移前の認証・プロジェクト確認
router.beforeEach(async (to) => {
  await authReady

  // 未ログインの場合
  if (to.meta.requiresAuth && !currentUser) {
    return { name: 'Login' }
  }

  // ログイン済みでログイン画面にアクセスした場合
  if (to.name === 'Login' && currentUser) {
    return { name: 'Home' }
  }

  // 認証が不要な画面、または未ログインの場合
  if (!to.meta.requiresAuth || !currentUser) {
    return true
  }

  try {
    // ユーザーがプロジェクトを登録しているか確認
    const hasProjects = await checkUserHasProjects(currentUser)

    // プロジェクトがない場合は登録画面へ
    if (!hasProjects && to.name !== 'ProjectRegister') {
      return { name: 'ProjectRegister' }
    }

    // プロジェクトがある場合は登録画面からホームへ
    if (hasProjects && to.name === 'ProjectRegister') {
      return { name: 'Home' }
    }

  } catch (error) {
    console.error('プロジェクト確認エラー:', error)

    // 確認に失敗した場合は登録画面へ
    if (to.name !== 'ProjectRegister') {
      return {
        name: 'ProjectRegister',
        query: { checkError: '1' }
      }
    }
  }

  return true
})

export default router
