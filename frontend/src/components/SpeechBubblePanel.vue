
<template>
  <div class="speech-bubble-panel">

    <div class="speech-bubble">
      <div class="bubble-content">
        <span class="bubble-title">
          コミュニケーションフィールド
        </span>

        <span class="bubble-description">
          メンバー間のコメントや連絡事項を表示します。
        </span>
      </div>
    </div>

  </div>
</template>

<script setup>
</script>

<style scoped>
* {
  box-sizing: border-box;
}

/* 吹き出し全体 */
.speech-bubble-panel {
  width: 100%;
  height: 100%;
  min-height: 0;

  display: flex;
  justify-content: center;
  align-items: center;

  padding-bottom: 24px;
}

/* 吹き出し本体 */
.speech-bubble {
  position: relative;

  width: 100%;
  height: 100%;
  min-height: 0;

  background: #ffffff;
  border: 2px solid #172033;
  border-radius: 25px;

  display: flex;
  justify-content: center;
  align-items: center;

  padding: 16px;
}

/* 吹き出しの三角形 */
.speech-bubble::after {
  content: "";

  position: absolute;
  top: 100%;
  left: 50%;
  transform: translateX(-50%);

  width: 0;
  height: 0;

  border-left: 35px solid transparent;
  border-right: 35px solid transparent;
  border-top: 24px solid white;

  z-index: 2;
}

/* 三角形の黒い枠線 */
.speech-bubble::before {
  content: "";

  position: absolute;
  top: calc(100% + 1px);
  left: 50%;
  transform: translateX(-50%);

  width: 0;
  height: 0;

  border-left: 38px solid transparent;
  border-right: 38px solid transparent;
  border-top: 27px solid #172033;

  z-index: 1;
}

/* 吹き出し内の文字 */
.bubble-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;

  width: 100%;
  text-align: center;
}

/* 見出し */
.bubble-title {
  color: #334155;
  font-size: clamp(14px, 1.3vw, 20px);
  font-weight: bold;
}

/* 説明 */
.bubble-description {
  color: #64748b;
  font-size: clamp(11px, 1vw, 14px);
  line-height: 1.6;
}

/* 小さい画面 */
@media (max-width: 1100px) {
  .speech-bubble {
    border-radius: 20px;
    padding: 10px;
  }

  .bubble-content {
    gap: 8px;
  }
}
</style>
