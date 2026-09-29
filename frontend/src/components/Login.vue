<template>
  <div class="auth-container">
    <h2>{{ isSignUp ? '新規アカウント登録' : 'ログイン' }}</h2>
    
    <form @submit.prevent="handleSubmit" class="auth-form">
      <div class="form-group">
        <label>メールアドレス</label>
        <input type="email" v-model="email" required placeholder="example@test.com" />
      </div>
      
      <div class="form-group">
        <label>パスワード</label>
        <input type="password" v-model="password" required placeholder="6文字以上" />
      </div>
      
      <button type="submit" class="submit-btn">
        {{ isSignUp ? '新規登録' : 'ログイン' }}
      </button>
    </form>

    <div class="toggle-mode">
      <span @click="isSignUp = !isSignUp">
        {{ isSignUp ? 'すでにアカウントをお持ちの方（ログインへ）' : '新規登録はこちら' }}
      </span>
    </div>

    <p v-if="errorMessage" class="error-msg">{{ errorMessage }}</p>
    
    <div v-if="user" class="success-box">
      <p>🎉 ログイン成功！</p>
      <p>ユーザー: {{ user.email }}</p>
      <p class="token-info">※ F12 のデベロッパーツール（Console）で Firebase ID Token を確認できます</p>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue';
import { auth } from '../firebase';
import { 
  createUserWithEmailAndPassword, 
  signInWithEmailAndPassword 
} from 'firebase/auth';

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
  max-width: 400px;
  margin: 40px auto;
  padding: 24px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background-color: #ffffff;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}

h2 {
  text-align: center;
  color: #1e293b;
  margin-bottom: 20px;
}

.form-group {
  margin-bottom: 16px;
  display: flex;
  flex-direction: column;
}

label {
  font-size: 14px;
  font-weight: bold;
  color: #475569;
  margin-bottom: 6px;
}

input {
  padding: 8px 12px;
  border: 1px solid #cbd5e1;
  border-radius: 4px;
  font-size: 14px;
}

.submit-btn {
  width: 100%;
  padding: 10px;
  background-color: #2563eb;
  color: white;
  border: none;
  border-radius: 4px;
  font-weight: bold;
  cursor: pointer;
  margin-top: 10px;
}

.submit-btn:hover {
  background-color: #1d4ed8;
}

.toggle-mode {
  text-align: center;
  margin-top: 16px;
  font-size: 13px;
}

.toggle-mode span {
  color: #2563eb;
  cursor: pointer;
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
</style>