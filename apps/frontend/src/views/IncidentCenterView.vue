<script setup lang="ts">
import AiopsView from "./AiopsView.vue";
</script>

<template>
  <section class="compat-workspace" aria-label="事件中心">
    <header class="compat-workspace__notice">
      <div><span>事件中心</span><strong>告警、调查与恢复闭环</strong></div>
      <p>当前先复用真实告警与诊断数据；事件队列将在下一切片替换此兼容视图。</p>
    </header>
    <div class="compat-workspace__content"><AiopsView /></div>
  </section>
</template>

<style scoped>
.compat-workspace { background: var(--surface-canvas); display: grid; grid-template-rows: auto minmax(0, 1fr); height: 100%; min-height: 0; }
.compat-workspace__notice { align-items: center; background: var(--surface-panel); border-bottom: 1px solid var(--line); display: flex; gap: 2rem; justify-content: space-between; padding: 0.8rem clamp(1rem, 2.5vw, 2rem); }
.compat-workspace__notice div { display: grid; gap: 0.15rem; }
.compat-workspace__notice span { color: var(--accent); font-size: 0.7rem; font-weight: 760; letter-spacing: 0.06em; text-transform: uppercase; }
.compat-workspace__notice strong { font-size: 0.9rem; }
.compat-workspace__notice p { color: var(--text-secondary); font-size: 0.75rem; margin: 0; max-width: 36rem; }
.compat-workspace__content { min-height: 0; overflow: hidden; }
@media (max-width: 760px) { .compat-workspace__notice { align-items: flex-start; flex-direction: column; gap: 0.35rem; } }
</style>
