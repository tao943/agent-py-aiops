<script setup lang="ts">
import { CheckCircle2, CircleDot, GitBranch } from "lucide-vue-next";
import type { AiopsDiagnosticEvidenceChain } from "@agent-py/api-contracts";
defineProps<{ readonly chain: AiopsDiagnosticEvidenceChain | null }>();
</script>

<template>
  <section class="trace" aria-label="执行链">
    <header><GitBranch :size="17" aria-hidden="true" /><div><h3>LangGraph 执行链</h3><p>仅展示持久化节点阶段与公开摘要，不展示隐藏推理。</p></div></header>
    <ol v-if="chain?.steps.length">
      <li v-for="step in chain.steps" :key="step.id">
        <component :is="step.status === 'completed' ? CheckCircle2 : CircleDot" :size="17" aria-hidden="true" />
        <div><strong>{{ step.phase }}</strong><span>{{ step.status }}</span><time :datetime="step.createdAt">{{ new Date(step.createdAt).toLocaleString('zh-CN') }}</time></div>
      </li>
    </ol>
    <p v-else class="empty">当前尚无已持久化的执行节点。</p>
  </section>
</template>

<style scoped>
.trace { padding: 1.1rem; }
header { align-items: flex-start; display: flex; gap: 0.6rem; }
header svg { color: var(--accent); margin-top: 0.12rem; }
h3 { font-size: 0.9rem; margin: 0; } header p, .empty { color: var(--text-secondary); font-size: 0.74rem; margin: 0.25rem 0 0; }
ol { list-style: none; margin: 1rem 0 0; padding: 0; }
li { display: grid; gap: 0.65rem; grid-template-columns: auto 1fr; padding: 0.65rem 0; }
li:not(:last-child) { border-bottom: 1px solid var(--line); }
li > svg { color: var(--success); margin-top: 0.08rem; }
li div { align-items: baseline; display: grid; gap: 0.35rem; grid-template-columns: minmax(8rem, 1fr) auto auto; }
strong { font-size: 0.78rem; overflow-wrap: anywhere; } span, time { color: var(--text-tertiary); font-size: 0.7rem; }
@media (max-width: 620px) { li div { grid-template-columns: 1fr auto; } time { grid-column: 1 / -1; } }
</style>
