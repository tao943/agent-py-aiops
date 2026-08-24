<script setup lang="ts">
import type { EvaluationRunSummary } from "@agent-py/api-contracts";
import AppBadge from "../../ui/AppBadge.vue";
defineProps<{ readonly items: readonly EvaluationRunSummary[] }>();
const live = (items: readonly EvaluationRunSummary[]) => items.filter((item) => item.mode === "live" || item.evaluationKind.includes("live"));
const statusLabel = (status: string) => ({ passed: "通过", failed: "失败", completed: "已完成", running: "运行中" }[status] ?? status);
</script>
<template>
  <section class="eval-summary" aria-label="评测历史">
    <header><span>EVALUATION</span><h2>已保存评测</h2></header>
    <div data-eval="live">
      <p v-if="live(items).length === 0">暂无已保存结果</p>
      <article v-for="item in live(items).slice(0, 5)" :key="item.runId"><span><strong>{{ item.scenarioId }}</strong><small>{{ item.evaluationKind }} · {{ item.completedAt ? new Date(item.completedAt).toLocaleString('zh-CN', { hour12: false }) : '未完成' }}</small></span><AppBadge :tone="item.passed === true ? 'success' : item.passed === false ? 'danger' : 'neutral'">{{ item.total === null ? statusLabel(item.status) : `${item.total} 分` }}</AppBadge></article>
    </div>
  </section>
</template>
<style scoped>
.eval-summary header { border-bottom: 1px solid var(--line); padding-bottom: 0.8rem; }.eval-summary header span { color: var(--accent); font-size: 0.62rem; font-weight: 800; letter-spacing: 0.1em; }.eval-summary h2 { font-size: 0.95rem; margin: 0.15rem 0 0; }.eval-summary p { color: var(--text-tertiary); font-size: 0.7rem; margin: 1rem 0; }.eval-summary article { align-items: center; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; min-height: 3.8rem; }.eval-summary article > span { display: grid; gap: 0.2rem; }.eval-summary strong { font-size: 0.74rem; }.eval-summary small { color: var(--text-tertiary); font-size: 0.64rem; }
</style>
