
<template>
  <div class="home-container">

    <!-- ========================= -->
    <!-- 左側のメニュー -->
    <!-- ========================= -->

    <aside class="left-panel">

      <div class="top-buttons">
        <button class="date-button">
          本日：○月○日○曜日
        </button>

        <button class="other-button">
          その他
        </button>
      </div>

      <!-- 期限が最も近いもの -->
      <button class="deadline-button">
        <span class="card-title">期限が最も近いもの</span>
        <span>○月○日期限</span>
        <span>プロジェクト：○○</span>
        <span>画面表示エラーの解決</span>
      </button>

      <!-- 期限が近いプロジェクト -->
      <button class="progress-button">
        <span>期限が近いプロジェクト</span>
        <span class="small-text">プロジェクトの進捗を確認</span>
      </button>

      <!-- プロジェクト一覧 -->
      <button class="project-list-button">
        <span>プロジェクト一覧</span>
        <span class="small-text">参加中のプロジェクトを確認</span>
      </button>

      <!-- エディタ -->
      <button class="editor-button">
        <span>エディタへ</span>
        <span class="small-text">コードの作成・編集</span>
      </button>

      <!-- 設定 -->
      <button class="setting-button">
        設定
      </button>

    </aside>


    <!-- ========================= -->
    <!-- 中央部分 -->
    <!-- ========================= -->

    <main class="center-panel">

      <div class="center-content">

        <!-- ホームタイトル -->
        <header class="home-header">
          <div>
            <h1>ホーム</h1>
            <p>チーム開発ワークスペース</p>
          </div>

          <span class="welcome-label">
            WORKSPACE
          </span>
        </header>


        <!-- プロジェクト概要 -->
        <section class="recent-project">

          <div class="project-header">
            <div>
              <span class="section-label">
                RECENT PROJECT
              </span>

              <h2>{{ projectName }}</h2>
            </div>

            <span class="status-badge">
              ● 編集中
            </span>
          </div>

          <div class="project-description">
            最後に編集したプロジェクト
          </div>

          <div class="project-meta">
            <span>最終編集</span>
            <strong>{{ lastEdited }}</strong>
          </div>

          <div class="project-divider"></div>


          <!-- メンバー一覧 -->
          <div class="member-section">

            <div class="member-header">
              <div>
                <h3>プロジェクトメンバー</h3>
                <p>このプロジェクトに参加しているメンバー</p>
              </div>

              <span class="member-count">
                {{ members.length }} 人
              </span>
            </div>


            <!-- メンバー表示 -->
            <div class="member-list">

              <div
                v-for="member in members"
                :key="member.id"
                class="member-item"
              >

                <div
                  class="member-avatar"
                  :style="{ backgroundColor: member.color }"
                >
                  {{ member.name.charAt(0) }}
                </div>

                <div class="member-info">
                  <strong>{{ member.name }}</strong>
                  <span>{{ member.role }}</span>
                </div>

              </div>

              <!-- メンバーがいない場合 -->
              <div
                v-if="members.length === 0"
                class="empty-members"
              >
                まだメンバーが登録されていません。
              </div>

            </div>


            <!-- メンバー追加ボタン -->
            <button
              class="add-member-button"
              @click="addMember"
            >
              <span class="plus-icon">＋</span>
              メンバーを追加
            </button>

          </div>

        </section>


        <!-- クイックアクセス -->
        <section class="quick-access">

          <h3>クイックアクセス</h3>

          <div class="quick-grid">

            <button class="quick-card">
              <span class="quick-icon">⌨</span>
              <strong>コード編集</strong>
              <small>エディタを開く</small>
            </button>

            <button class="quick-card">
              <span class="quick-icon">☑</span>
              <strong>タスク管理</strong>
              <small>作業状況を確認</small>
            </button>

            <button class="quick-card">
              <span class="quick-icon">▤</span>
              <strong>プロジェクト</strong>
              <small>一覧を確認</small>
            </button>

            <button class="quick-card">
              <span class="quick-icon">♧</span>
              <strong>メンバー</strong>
              <small>参加者を確認</small>
            </button>

          </div>

        </section>

      </div>

    </main>


    <!-- ========================= -->
    <!-- 右側のコミュニケーション欄 -->
    <!-- 独立コンポーネント -->
    <!-- ========================= -->

    <CommunicationPanel />

  </div>
</template>


<script setup>

import { ref } from "vue";
import CommunicationPanel from "./CommunicationPanel.vue";


// =========================
// プロジェクト情報
// =========================

const projectName = ref("画面表示エラーの解決方法");

const lastEdited = ref("2026年11月9日");


// =========================
// メンバー情報
// =========================

const members = ref([

  {
    id: 1,
    name: "伊藤",
    role: "管理者",
    color: "#2563eb"
  },

  {
    id: 2,
    name: "田中",
    role: "メンバー",
    color: "#0d9488"
  },

  {
    id: 3,
    name: "佐藤",
    role: "メンバー",
    color: "#9333ea"
  }

]);


// =========================
// メンバー追加機能
// =========================

function addMember() {

  const name = window.prompt(
    "追加するメンバーの名前を入力してください"
  );

  // キャンセル・空欄の場合
  if (!name || !name.trim()) {
    return;
  }

  const trimmedName = name.trim();

  // 同じ名前の重複登録を防止
  const exists = members.value.some(
    member => member.name === trimmedName
  );

  if (exists) {
    window.alert("このメンバーはすでに登録されています。");
    return;
  }

  // メンバーを追加
  members.value.push({

    id: Date.now(),

    name: trimmedName,

    role: "メンバー",

    color: "#2563eb"

  });

  window.alert(
    trimmedName + " さんをメンバーに追加しました。"
  );

}

</script>


<style scoped>

/* ========================= */
/* 全体設定 */
/* ========================= */

* {
  box-sizing: border-box;
}

.home-container {

  display: flex;

  width: 100%;
  height: 100vh;
  height: 100dvh;

  min-height: 0;

  background: #f1f5f9;

  font-family:
    "Noto Sans JP",
    "Yu Gothic",
    sans-serif;

  overflow: hidden;

}


/* ========================= */
/* 左側メニュー */
/* ========================= */

.left-panel {

  flex: 0 0 380px;
  width: 380px;

  height: 100%;

  background: #ffffff;

  padding: 13px 18px;

  display: flex;
  flex-direction: column;
  align-items: flex-start;

  border-right: 1px solid #e2e8f0;

  overflow-y: auto;

}


/* 共通ボタン */

button {

  cursor: pointer;

  font-family: inherit;

  transition:
    background 0.2s,
    transform 0.2s;

}

button:hover {
  filter: brightness(0.96);
}


/* 上部ボタン */

.top-buttons {

  display: flex;

  gap: 0;

  width: 100%;

  height: 33px;

  flex-shrink: 0;

}

.date-button {

  width: 255px;
  height: 33px;

  background: #f1f5f9;

  border: 1px solid #cbd5e1;

  border-radius: 5px 0 0 5px;

}

.other-button {

  width: 86px;
  height: 33px;

  background: #e2e8f0;

  border: 1px solid #cbd5e1;

  border-left: none;

  border-radius: 0 5px 5px 0;

}


/* 期限が最も近いもの */

.deadline-button {

  width: 100%;
  height: 143px;

  margin-top: 12px;

  flex-shrink: 0;

  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;

  gap: 8px;

  background: #eff6ff;

  border: 1px solid #bfdbfe;

  border-radius: 9px;

  color: #1e293b;

  font-size: 14px;

}

.card-title {
  font-size: 16px;
  font-weight: bold;
}


/* 期限が近いプロジェクト */

.progress-button {

  width: 100%;
  height: 110px;

  margin-top: 13px;

  flex-shrink: 0;

  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;

  gap: 9px;

  background: #f8fafc;

  border: 1px solid #cbd5e1;

  border-radius: 9px;

  font-size: 16px;
  font-weight: bold;

}


/* プロジェクト一覧 */

.project-list-button {

  width: 100%;
  height: 100px;

  margin-top: 12px;

  flex-shrink: 0;

  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;

  gap: 8px;

  background: #f8fafc;

  border: 1px solid #cbd5e1;

  border-radius: 9px;

  font-size: 17px;
  font-weight: bold;

}


/* エディタ */

.editor-button {

  width: 244px;
  height: 95px;

  margin-top: 16px;
  margin-left: 8px;

  flex-shrink: 0;

  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;

  gap: 8px;

  background: #dbeafe;

  border: 1px solid #93c5fd;

  border-radius: 9px;

  font-size: 17px;
  font-weight: bold;

}


/* 小さい説明文字 */

.small-text {

  font-size: 11px;

  font-weight: normal;

  color: #64748b;

}


/* 設定 */

.setting-button {

  width: 87px;
  height: 39px;

  margin-top: auto;
  margin-bottom: 10px;

  margin-left: 7px;

  flex-shrink: 0;

  background: #f1f5f9;

  border: 1px solid #cbd5e1;

  border-radius: 6px;

}


/* ========================= */
/* 中央部分 */
/* ========================= */

.center-panel {

  flex: 1;

  min-width: 0;
  min-height: 0;

  height: 100%;

  background: #f1f5f9;

  overflow-y: auto;

  padding: 30px;

}


.center-content {

  width: 100%;
  max-width: 850px;

  margin: 0 auto;

}


/* ホームヘッダー */

.home-header {

  display: flex;

  justify-content: space-between;
  align-items: center;

  margin-bottom: 25px;

}

.home-header h1 {

  font-size: 27px;

  color: #1e293b;

  margin: 0;

}

.home-header p {

  color: #64748b;

  font-size: 13px;

  margin: 7px 0 0;

}

.welcome-label {

  font-size: 11px;

  font-weight: bold;

  letter-spacing: 2px;

  color: #64748b;

  background: #e2e8f0;

  padding: 9px 13px;

  border-radius: 6px;

}


/* ========================= */
/* 最近編集したプロジェクト */
/* ========================= */

.recent-project {

  width: 100%;

  background: #ffffff;

  padding: 24px;

  border: 1px solid #e2e8f0;

  border-radius: 12px;

  box-shadow:
    0 3px 10px rgba(15, 23, 42, 0.04);

}


.project-header {

  display: flex;

  justify-content: space-between;
  align-items: flex-start;

  gap: 12px;

}

.section-label {

  color: #64748b;

  font-size: 10px;

  letter-spacing: 1.5px;

  font-weight: bold;

}

.project-header h2 {

  margin: 9px 0 0;

  font-size: 21px;

  color: #1e293b;

  overflow-wrap: anywhere;

}


.status-badge {

  flex-shrink: 0;

  color: #15803d;

  background: #dcfce7;

  padding: 7px 11px;

  border-radius: 20px;

  font-size: 11px;

  font-weight: bold;

}


.project-description {

  color: #64748b;

  font-size: 12px;

  margin-top: 15px;

}


.project-meta {

  display: flex;

  justify-content: space-between;

  align-items: center;

  gap: 10px;

  margin-top: 12px;

  font-size: 12px;

  color: #64748b;

}

.project-meta strong {

  color: #334155;

  font-weight: 500;

}


.project-divider {

  height: 1px;

  background: #e2e8f0;

  margin: 20px 0;

}


/* ========================= */
/* メンバー一覧 */
/* ========================= */

.member-section {

  width: 100%;

}


.member-header {

  display: flex;

  justify-content: space-between;
  align-items: center;

  gap: 10px;

}

.member-header h3 {

  margin: 0;

  color: #1e293b;

  font-size: 16px;

}

.member-header p {

  margin: 6px 0 0;

  color: #94a3b8;

  font-size: 11px;

}


.member-count {

  flex-shrink: 0;

  color: #2563eb;

  background: #eff6ff;

  padding: 6px 11px;

  border-radius: 20px;

  font-size: 12px;

  font-weight: bold;

}


/* メンバーリスト */

.member-list {

  display: flex;

  flex-direction: column;

  gap: 13px;

  margin-top: 20px;

}


.member-item {

  display: flex;

  align-items: center;

  gap: 12px;

  padding: 9px;

  background: #f8fafc;

  border: 1px solid #f1f5f9;

  border-radius: 8px;

}


.member-avatar {

  width: 40px;
  height: 40px;

  flex-shrink: 0;

  border-radius: 50%;

  display: flex;

  justify-content: center;
  align-items: center;

  color: #ffffff;

  font-size: 16px;

  font-weight: bold;

}


.member-info {

  display: flex;

  flex-direction: column;

  gap: 4px;

}

.member-info strong {

  font-size: 13px;

  color: #334155;

}

.member-info span {

  font-size: 11px;

  color: #64748b;

}


.empty-members {

  color: #94a3b8;

  font-size: 13px;

  padding: 15px 0;

}


/* メンバー追加ボタン */

.add-member-button {

  width: 100%;

  height: 43px;

  margin-top: 18px;

  background: #2563eb;

  color: #ffffff;

  border: none;

  border-radius: 7px;

  font-size: 13px;

  font-weight: bold;

}

.add-member-button:hover {

  background: #1d4ed8;

}


.plus-icon {

  font-size: 18px;

  margin-right: 5px;

}


/* ========================= */
/* クイックアクセス */
/* ========================= */

.quick-access {

  margin-top: 25px;

  padding-bottom: 20px;

}


.quick-access h3 {

  color: #1e293b;

  font-size: 16px;

  margin: 0 0 14px;

}


.quick-grid {

  display: grid;

  grid-template-columns: repeat(2, minmax(0, 1fr));

  gap: 12px;

}


.quick-card {

  min-height: 90px;

  padding: 13px;

  display: flex;

  flex-direction: column;

  align-items: flex-start;

  justify-content: center;

  gap: 5px;

  background: #ffffff;

  border: 1px solid #e2e8f0;

  border-radius: 9px;

  text-align: left;

}


.quick-card:hover {

  background: #eff6ff;

  border-color: #93c5fd;

}


.quick-icon {

  color: #2563eb;

  font-size: 21px;

}


.quick-card strong {

  color: #334155;

  font-size: 13px;

}


.quick-card small {

  color: #94a3b8;

  font-size: 10px;

}


/* ========================= */
/* 画面幅への対応 */
/* ========================= */

@media (max-width: 1100px) {

  .left-panel {

    flex-basis: 300px;

    width: 300px;

    padding: 12px;

  }

  .date-button {

    width: calc(100% - 86px);

  }

  .other-button {

    width: 86px;

  }

  .center-panel {

    padding: 20px;

  }

  .home-header h1 {

    font-size: 23px;

  }

  .recent-project {

    padding: 18px;

  }

}


/* ========================= */
/* 高さが小さい画面 */
/* ========================= */

@media (max-height: 650px) {

  .center-panel {

    padding-top: 15px;
    padding-bottom: 15px;

  }

  .home-header {

    margin-bottom: 15px;

  }

  .home-header h1 {

    font-size: 22px;

  }

  .recent-project {

    padding: 16px;

  }

  .project-divider {

    margin: 12px 0;

  }

  .member-list {

    gap: 7px;

    margin-top: 12px;

  }

  .member-item {

    padding: 6px;

  }

  .member-avatar {

    width: 32px;
    height: 32px;

  }

  .add-member-button {

    height: 36px;

    margin-top: 12px;

  }

  .quick-access {

    margin-top: 15px;

  }

  .quick-card {

    min-height: 70px;

  }

}


/* ========================= */
/* スマートフォン対応 */
/* ========================= */

@media (max-width: 700px) {

  .left-panel {

    flex-basis: 180px;

    width: 180px;

    padding: 8px;

  }

  .deadline-button {

    font-size: 11px;

    height: 120px;

  }

  .progress-button,
  .project-list-button {

    font-size: 12px;

  }

  .editor-button {

    width: 100%;

    margin-left: 0;

  }

  .center-panel {

    padding: 12px;

  }

  .home-header h1 {

    font-size: 20px;

  }

  .welcome-label {

    display: none;

  }

  .recent-project {

    padding: 12px;

  }

  .project-header h2 {

    font-size: 16px;

  }

}

</style>
