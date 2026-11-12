
<template>
  <aside class="communication-panel">

    <div class="outer-panel">

      <!-- パネル上部 -->
      <div class="panel-header">
        <span class="header-icon">●</span>
        コミュニケーション
      </div>

      <!-- プロジェクト名 -->
      <button class="project-name-button">
        <span class="project-label">PROJECT</span>
        <span>プロジェクト名：○○</span>
      </button>

      <!-- コミュニケーション欄 -->
      <div class="communication-area">
        <SpeechBubblePanel />
      </div>

      <!-- 最終変更情報 -->
      <div class="update-area">
        <div class="update-title">最終更新情報</div>

        <div class="update-info">
          <span>最終変更</span>
          <strong>○○時間前</strong>
        </div>

        <div class="update-info">
          <span>変更者</span>
          <strong>○○</strong>
        </div>
      </div>

    </div>

  </aside>
</template>

<script setup>
import SpeechBubblePanel from "./SpeechBubblePanel.vue";
</script>

<style scoped>
* {
  box-sizing: border-box;
}

/* 右側全体 */
.communication-panel {
  flex: 0 1 360px;
  width: 360px;
  min-width: 0;
  height: 100%;
  min-height: 0;
  padding: 16px;
  background: #e2e8f0;

  display: flex;
  justify-content: center;
  align-items: center;

  overflow: hidden;
}

/* 外側のパネル */
.outer-panel {
  width: 100%;
  max-width: 366px;
  height: 100%;
  max-height: 650px;
  min-height: 0;

  padding: 18px;
  background: #17677d;
  border-radius: 12px;

  display: flex;
  flex-direction: column;
  gap: 16px;

  overflow: hidden;
}

/* パネル見出し */
.panel-header {
  height: 30px;
  flex-shrink: 0;
  color: white;
  font-size: 15px;
  font-weight: bold;

  display: flex;
  align-items: center;
  gap: 10px;
}

.header-icon {
  color: #60a5fa;
}

/* プロジェクト名 */
.project-name-button {
  width: 100%;
  min-height: 72px;
  flex-shrink: 0;

  border: none;
  border-radius: 8px;
  background: white;
  color: #172033;

  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 5px;

  font-size: 18px;
  font-weight: bold;
  cursor: pointer;
}

.project-label {
  color: #64748b;
  font-size: 10px;
  letter-spacing: 2px;
}

/* コミュニケーション欄 */
.communication-area {
  flex: 1;
  min-height: 120px;
  width: 100%;
  overflow: hidden;
}

/* 最終更新情報 */
.update-area {
  width: 100%;
  flex-shrink: 0;

  padding: 12px 16px;
  background: white;
  border-radius: 8px;

  display: flex;
  flex-direction: column;
  gap: 10px;
}

.update-title {
  color: #64748b;
  font-size: 12px;
  font-weight: bold;
}

.update-info {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;

  color: #334155;
  font-size: 14px;
}

.update-info strong {
  color: #172033;
}

/* 狭い画面 */
@media (max-width: 1100px) {
  .communication-panel {
    flex-basis: 300px;
    width: 300px;
    padding: 10px;
  }

  .outer-panel {
    padding: 12px;
    gap: 12px;
  }

  .project-name-button {
    font-size: 15px;
  }
}

/* 画面の高さが小さい場合 */
@media (max-height: 650px) {
  .communication-panel {
    padding: 8px;
  }

  .outer-panel {
    padding: 10px;
    gap: 8px;
  }

  .project-name-button {
    min-height: 55px;
  }

  .update-area {
    padding: 8px 12px;
    gap: 5px;
  }
}
</style>
