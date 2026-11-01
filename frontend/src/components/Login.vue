<template>
  <div class="auth-container">
    <h2>{{ isSignUp ? '新規アカウント登録' : 'ログイン' }}</h2>
    
    <form @submit.prevent="handleSubmit" class="auth-form">
      <div class="form-group">
        <label for="auth-email">メールアドレス</label>
        <input id="auth-email" type="email" v-model="email" required autocomplete="email" placeholder="example@test.com" />
      </div>
      
      <div class="form-group">
        <label for="auth-password">パスワード</label>
        <input id="auth-password" type="password" v-model="password" required :autocomplete="isSignUp ? 'new-password' : 'current-password'" placeholder="6文字以上" />
      </div>
      
      <button type="submit" class="submit-btn">
        {{ isSignUp ? '新規登録' : 'ログイン' }}
      </button>
    </form>

    <div class="toggle-mode">
      <button type="button" @click="isSignUp = !isSignUp">
        {{ isSignUp ? 'すでにアカウントをお持ちの方（ログインへ）' : '新規登録はこちら' }}
      </button>
    </div>

    <p v-if="errorMessage" class="error-msg" role="alert">{{ errorMessage }}</p>
    
    <div v-if="user" class="success-box">
      <p>🎉 ログイン成功！</p>
      <p>ユーザー: {{ user.email }}</p>
      <p class="token-info">※ F12 のデベロッパーツール（Console）で Firebase ID Token を確認できます</p>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { auth } from '../firebase';
import {
  createUserWithEmailAndPassword,
  signInWithEmailAndPassword
} from 'firebase/auth';

const router = useRouter();

const email = ref('');
const password = ref('');
const isSignUp = ref(false);
const user = ref(null);
const errorMessage = ref('');

const handleSubmit = async () => {
  errorMessage.value = '';
  try {
    if (isSignUp.value) {
      // 1. 新規アカウント作成
      const res = await createUserWithEmailAndPassword(auth, email.value, password.value);
      user.value = res.user;
    } else {
      // 2. ログイン処理
      const res = await signInWithEmailAndPassword(auth, email.value, password.value);
      user.value = res.user;
      router.push('/')
    }

    // バックエンド検証用の Firebase ID トークン (JWT) を取得
    const idToken = await user.value.getIdToken();
    console.log("=== Firebase ID Token ===");
    console.log(idToken);

  } catch (err) {
    console.error(err);
    errorMessage.value = getJapaneseErrorMessage(err.code);
  }
};

// エラーメッセージの日本語化
const getJapaneseErrorMessage = (code) => {
  switch (code) {
    case 'auth/email-already-in-use':
      return 'このメールアドレスは既に登録されています。';
    case 'auth/invalid-email':
      return 'メールアドレスの形式が正しくありません。';
    case 'auth/weak-password':
      return 'パスワードは6文字以上で入力してください。';
    case 'auth/user-not-found':
    case 'auth/wrong-password':
    case 'auth/invalid-credential':
      return 'メールアドレスまたはパスワードが間違っています。';
    default:
      return '認証エラーが発生しました。';
  }
};
</script>

<style scoped>
.auth-container {
  width: min(100% - 32px, 400px);
  box-sizing: border-box;
  margin: 40px auto;
  padding: 32px;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  background-color: #ffffff;
  box-shadow: 0 18px 45px -28px rgba(15, 23, 42, 0.42), 0 2px 8px rgba(15, 23, 42, 0.04);
}

h2 {
  text-align: center;
  color: #1e293b;
  margin: 0 0 28px;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 0;
}

.form-group {
  margin-bottom: 22px;
  display: flex;
  flex-direction: column;
}

label {
  font-size: 13px;
  font-weight: 600;
  color: #475569;
  margin-bottom: 8px;
}

input {
  width: 100%;
  box-sizing: border-box;
  min-height: 46px;
  padding: 10px 13px;
  border: 1px solid #cbd5e1;
  border-radius: 7px;
  font-size: 14px;
  color: #1e293b;
  background: #fff;
  transition: border-color 160ms ease, box-shadow 160ms ease;
}

input::placeholder {
  color: #94a3b8;
}

input:focus-visible {
  outline: none;
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.14);
}

.submit-btn {
  width: 100%;
  min-height: 46px;
  padding: 11px 16px;
  background-color: #2563eb;
  color: white;
  border: none;
  border-radius: 7px;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  margin-top: 2px;
  transition: background-color 160ms ease, box-shadow 160ms ease, transform 160ms ease;
}

.submit-btn:hover {
  background-color: #1d4ed8;
  box-shadow: 0 5px 12px rgba(37, 99, 235, 0.2);
  transform: translateY(-1px);
}

.submit-btn:focus-visible,
.toggle-mode button:focus-visible {
  outline: 3px solid rgba(37, 99, 235, 0.28);
  outline-offset: 3px;
}

.toggle-mode {
  text-align: center;
  margin-top: 20px;
  font-size: 13px;
}

.toggle-mode button {
  padding: 4px;
  border: 0;
  background: transparent;
  color: #2563eb;
  cursor: pointer;
  font: inherit;
}

.toggle-mode button:hover {
  color: #1d4ed8;
  text-decoration: underline;
}

.error-msg {
  color: #ef4444;
  font-size: 14px;
  margin-top: 12px;
  text-align: center;
}

.success-box {
  margin-top: 20px;
  padding: 12px;
  background-color: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: 4px;
  color: #166534;
}

.token-info {
  font-size: 11px;
  color: #15803d;
  margin-top: 4px;
}

@media (max-width: 480px) {
  .auth-container {
    margin: 24px auto;
    padding: 26px 22px;
  }
}

@media (prefers-reduced-motion: reduce) {
  input,
  .submit-btn {
    transition: none;
  }
}
</style>