<template>
  <div class="home-container">
    <h1>ホーム画面</h1>
    <button @click="handleLogout">ログアウト</button>
    <button @click="gotoSchedule">スケジュール画面</button>
    <button @click="gotoEditor">エディター画面</button>
    <button @click="gotoCodoSharing">コード共有画面</button>
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
const gotoSchedule=()=>{
  router.push('/schedule')
}
const gotoEditor=()=>{
  router.push('/Editor')
}
const gotoCodoSharing=()=>{
  router.push('/CodeSharing')
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