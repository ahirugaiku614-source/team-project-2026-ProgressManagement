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
import {
  checkUserHasProjects,
  clearProjectStatusCache
} from './services/projectSetup'

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

// 認証状態の変更を検出する番号
let authVersion = 0

// 初回認証確認を待機するPromise
const authReady = new Promise((resolve) => {
  resolveAuthReady = resolve
})

// ログイン・ログアウト状態を監視
onAuthStateChanged(auth, (user) => {
  currentUser = user

  // 古いプロジェクト確認処理を無効化
  authVersion += 1
  clearProjectStatusCache()

  if (!authInitialized) {
    authInitialized = true
    resolveAuthReady()
  }
}, () => {
  currentUser = null
  authVersion += 1
  clearProjectStatusCache()

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

  // 認証が不要な画面
  if (!to.meta.requiresAuth) {
    return true
  }

  // 確認開始時のユーザーと認証状態を保存
  const checkingUser = currentUser
  const checkingVersion = authVersion

  const authChanged = () => {
    return (
      authVersion !== checkingVersion ||
      auth.currentUser?.uid !== checkingUser.uid
    )
  }

  try {
    // 最新のプロジェクト登録状況を取得
    const hasProjects =
      await checkUserHasProjects(checkingUser)

    // 確認中にログアウトやアカウント変更が発生
    if (authChanged()) {
      return auth.currentUser
        ? { name: 'Home' }
        : { name: 'Login' }
    }

    // プロジェクト登録済み
    if (hasProjects) {
      if (to.name === 'ProjectRegister') {
        return { name: 'Home' }
      }

      return true
    }

    // 正常に確認でき、プロジェクトが存在しない
    if (to.name !== 'ProjectRegister') {
      return { name: 'ProjectRegister' }
    }

    // 通信エラー状態から復旧した場合
    if (to.query.checkError === '1') {
      return {
        name: 'ProjectRegister',
        replace: true
      }
    }

    return true

  } catch (error) {
    // 確認中に認証状態が変更された場合
    if (authChanged()) {
      return auth.currentUser
        ? { name: 'Home' }
        : { name: 'Login' }
    }

    console.error('プロジェクト情報の確認に失敗しました。')

    // 登録状況が不明な場合はエラー状態の登録画面へ
    if (
      to.name !== 'ProjectRegister' ||
      to.query.checkError !== '1'
    ) {
      return {
        name: 'ProjectRegister',
        query: { checkError: '1' },
        replace: true
      }
    }

    // 同じエラー画面への無限リダイレクトを防止
    return true
  }
})

export default router