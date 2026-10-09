<template>
  <div class="auth-container">
    <div class="auth-brand"><span class="auth-brand-mark">T</span><span>TeamFlow</span></div>
    <p class="auth-eyebrow">TEAM DEVELOPMENT PLATFORM</p>
    <h2>{{ isSignUp ? 'アカウントを作成' : 'おかえりなさい' }}</h2>
    <p class="auth-description">{{ isSignUp ? 'チームの進捗管理を始めましょう。' : 'ログインしてプロジェクトの進捗を確認しましょう。' }}</p>
    <form @submit.prevent="handleSubmit" class="auth-form">
      <div class="form-group">
        <label for="email">メールアドレス</label>
        <input id="email" type="email" v-model="email" required autocomplete="email" placeholder="example@test.com" />
      </div>
      <div class="form-group">
        <label for="password">パスワード</label>
        <input id="password" type="password" v-model="password" required minlength="6" :autocomplete="isSignUp ? 'new-password' : 'current-password'" placeholder="6文字以上" />
      </div>
      <button type="submit" class="submit-btn" :disabled="loading">{{ loading ? '処理中…' : (isSignUp ? '新規登録する' : 'ログインする') }}</button>
    </form>
    <p v-if="errorMessage" class="error-msg">{{ errorMessage }}</p>
    <div class="toggle-mode">
      <span>{{ isSignUp ? 'すでにアカウントをお持ちですか？' : 'アカウントをお持ちでないですか？' }}</span>
      <button type="button" @click="toggleMode">{{ isSignUp ? 'ログイン' : '新規登録' }}</button>
    </div>
    <p class="auth-footer">安全なFirebase認証でチームワークをサポートします。</p>
  </div>
</template>

<script setup>
import { ref } from 'vue';
import { auth } from '../firebase';
import { createUserWithEmailAndPassword, signInWithEmailAndPassword } from 'firebase/auth';

const email = ref('');
const password = ref('');
const isSignUp = ref(false);
const loading = ref(false);
const errorMessage = ref('');

const toggleMode = () => {
  isSignUp.value = !isSignUp.value;
  errorMessage.value = '';
};
const handleSubmit = async () => {
  if (loading.value) return;
  loading.value = true;
  errorMessage.value = '';
  try {
    if (isSignUp.value) {
      await createUserWithEmailAndPassword(auth, email.value.trim(), password.value);
    } else {
      await signInWithEmailAndPassword(auth, email.value.trim(), password.value);
    }
    // App.vue observes Firebase auth state and automatically displays the dashboard.
  } catch (err) {
    console.error('Firebase authentication failed:', err);
    errorMessage.value = getJapaneseErrorMessage(err.code);
  } finally {
    loading.value = false;
  }
};
const getJapaneseErrorMessage = (code) => {
  switch (code) {
    case 'auth/email-already-in-use': return 'このメールアドレスは既に登録されています。';
    case 'auth/invalid-email': return 'メールアドレスの形式が正しくありません。';
    case 'auth/weak-password': return 'パスワードは6文字以上で入力してください。';
    case 'auth/user-not-found':
    case 'auth/wrong-password':
    case 'auth/invalid-credential': return 'メールアドレスまたはパスワードが間違っています。';
    case 'auth/too-many-requests': return 'ログイン試行が多すぎます。しばらくしてから再試行してください。';
    case 'auth/network-request-failed': return 'ネットワーク接続を確認してください。';
    default: return '認証エラーが発生しました。Firebaseの設定を確認してください。';
  }
};
</script>

<style scoped>
.auth-container{box-sizing:border-box;width:min(100% - 32px,420px);margin:7vh auto;padding:36px;background:#fff;border:1px solid #ececf4;border-radius:18px;box-shadow:0 18px 60px #292d440c;color:#282b43;text-align:left;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.auth-brand{display:flex;align-items:center;gap:10px;font-size:20px;font-weight:800;letter-spacing:-.5px}
.auth-brand-mark{display:grid;place-items:center;width:34px;height:34px;background:#6558e8;color:#fff;border-radius:10px}
.auth-eyebrow{font-size:10px;font-weight:800;letter-spacing:1.2px;color:#7b70e8;margin:28px 0 8px}
h2{font-size:25px;font-weight:800;letter-spacing:-.7px;color:#292c43;margin:0 0 8px;text-align:left}
.auth-description{font-size:12px;color:#9295a8;margin:0 0 24px}
.form-group{margin-bottom:17px;display:flex;flex-direction:column}
label{font-size:12px;font-weight:700;color:#555970;margin-bottom:7px}
input{box-sizing:border-box;width:100%;padding:12px 13px;border:1px solid #e0e2ed;border-radius:8px;font-size:13px;color:#33374f;background:#fff;outline:none}
input:focus{border-color:#867aef;box-shadow:0 0 0 3px #867aef1c}
.submit-btn{width:100%;padding:12px;background:#6558e8;color:white;border:0;border-radius:8px;font-size:13px;font-weight:750;cursor:pointer;margin-top:6px}
.submit-btn:hover{background:#5447d6}.submit-btn:disabled{opacity:.65;cursor:wait}
.toggle-mode{display:flex;justify-content:center;align-items:center;gap:5px;margin-top:20px;font-size:11px;color:#9295a8;flex-wrap:wrap}
.toggle-mode button{border:0;background:transparent;color:#6558e8;font-size:11px;font-weight:750;cursor:pointer}
.error-msg{color:#dc4545;font-size:12px;margin-top:14px;padding:10px;background:#fff1f1;border-radius:7px}
.auth-footer{font-size:10px;color:#b0b2c0;text-align:center;border-top:1px solid #f0f0f5;padding-top:18px;margin-top:26px}
@media(max-width:480px){.auth-container{padding:25px}}
</style>
