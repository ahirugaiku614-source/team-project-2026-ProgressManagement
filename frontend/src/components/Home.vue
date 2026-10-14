<template>
  <div class="home-container">
    <h1>ホーム画面</h1>
    <button @click="handleLogout">ログアウト</button>
  </div>
</template>

<script setup>
import { getAuth, signOut } from 'firebase/auth'
import { useRouter } from 'vue-router'

const router = useRouter()
const auth = getAuth()

const handleLogout = async () => {
  try {
    await signOut(auth)
    router.push('/login')
  } catch (error) {
    console.error('ログアウトエラー:', error)
  }
}
</script>

<style scoped>
.home-container {
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  height: 80vh;
  gap: 20px;
}
</style>