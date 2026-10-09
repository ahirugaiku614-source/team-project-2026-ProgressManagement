<template>
  <main class="app-shell">
    <div v-if="!authReady" class="loading-screen">ログイン状態を確認しています…</div>
    <Login v-else-if="!currentUser" />
    <Dashboard v-else :user="currentUser" @logout="currentUser = null" />
  </main>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue';
import { onAuthStateChanged } from 'firebase/auth';
import { auth } from './firebase';
import Login from './components/Login.vue';
import Dashboard from './components/Dashboard.vue';

const currentUser = ref(null);
const authReady = ref(false);
let unsubscribe;

onMounted(() => {
  unsubscribe = onAuthStateChanged(auth, (user) => {
    currentUser.value = user;
    authReady.value = true;
  });
});
onUnmounted(() => unsubscribe?.());
</script>

<style>
.app-shell { min-height: 100vh; width: 100%; }
.loading-screen { min-height: 100vh; display: grid; place-items: center; color: #777b91; font-size: 14px; }
</style>
