import { createRouter, createWebHistory } from 'vue-router'
import { onAuthStateChanged } from 'firebase/auth'
import { auth } from './firebase'
import Home from './components/Home.vue'
import Login from './components/Login.vue'

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
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

// 未ログインユーザーの直リンク防止（ナビゲーションガード）
router.beforeEach((to, from, next) => {
  
  const unsubscribe = onAuthStateChanged(auth, (user) => {
    unsubscribe()
    if (to.meta.requiresAuth && !user) {
      next('/login')
    } else {
      next()
    }
  })
})

export default router