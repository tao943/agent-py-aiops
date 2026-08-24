<script setup lang="ts">
import { Network, ShieldCheck } from "lucide-vue-next";
import type { AiopsDiagnosticEvidenceChain } from "@agent-py/api-contracts";
import type { PublicInvestigationResult } from "../../stores/aiops";
import AppBadge from "../../ui/AppBadge.vue";

defineProps<{ readonly chain: AiopsDiagnosticEvidenceChain | null; readonly result: PublicInvestigationResult }>();

function tone(status: string): "success" | "danger" | "warning" | "neutral" {
  if (status === "supported") return "success";
  if (status === "refuted" || status === "failed") return "danger";
  if (status === "inconclusive" || status === "timeout") return "warning";
  return "neutral";
}
</script>

<template>
  <section class="hypothesis" aria-label="假设与证据">
    <div class="hypothesis__decision">
      <header><ShieldCheck :size="17" aria-hidden="true" /><div><h3>证据化结论</h3><p>未知字段保持未知，不从缺失值推断成功。</p></div></header>
      <dl><div><dt>根因结论</dt><dd>{{ result.rootCauseSummary }}</dd></div><div><dt>恢复提案</dt><dd>{{ result.recoverySummary }}</dd></div></dl>
      <p :data-validator="result.validatorOrigin">{{ result.validatorMessage }}</p>
      <p :data-recovery-permitted="String(result.executionPermitted)">{{ result.executionPermitted === false ? "禁止自动执行" : result.executionPermitted === true ? "允许进入受控恢复" : "执行权限未提供" }}</p>
    </div>
    <div v-if="result.agentMode === 'multi'" class="hypothesis__specialists">
      <header><Network :size="17" aria-hidden="true" /><div><h3>Specialist 调查结果</h3><p>各分支只提交公开证据摘要，由主链统一裁决。</p></div></header>
      <ul><li v-for="item in result.specialists" :key="item.role" :data-status="item.status"><div><strong>{{ item.role }}</strong><AppBadge :tone="tone(item.status)">{{ item.status }}</AppBadge></div><p>{{ item.safeSummary }}</p></li></ul>
      <p v-if="result.specialists.length === 0" class="empty">Multi-Agent 路由已启用，但未提供可公开的分支结果。</p>
    </div>
    <div class="hypothesis__evidence">
      <header><ShieldCheck :size="17" aria-hidden="true" /><div><h3>公开证据</h3><p>只展示服务端持久化的证据摘要与来源。</p></div></header>
      <ul v-if="chain?.evidence.length"><li v-for="evidence in chain.evidence" :key="evidence.id"><div><strong>{{ evidence.source }}</strong><AppBadge>{{ evidence.kind }}</AppBadge></div><p>{{ evidence.summary }}</p><time :datetime="evidence.createdAt">{{ new Date(evidence.createdAt).toLocaleString('zh-CN') }}</time></li></ul>
      <p v-else class="empty">当前尚无可公开的持久化证据。</p>
    </div>
  </section>
</template>

<style scoped>
.hypothesis { min-width: 0; }
.hypothesis__decision, .hypothesis__specialists, .hypothesis__evidence { border-bottom: 1px solid var(--line); padding: 1.1rem; }
header { align-items: flex-start; display: flex; gap: 0.55rem; } header > svg { color: var(--accent); margin-top: 0.08rem; }
h3 { font-size: 0.9rem; margin: 0; } header p, .empty { color: var(--text-secondary); font-size: 0.72rem; margin: 0.25rem 0 0; }
dl { display: grid; gap: 0; margin: 0.9rem 0; } dl div { border-top: 1px solid var(--line); display: grid; gap: 0.8rem; grid-template-columns: 7rem minmax(0, 1fr); padding: 0.7rem 0; }
dt { color: var(--text-tertiary); font-size: 0.72rem; } dd { font-size: 0.78rem; line-height: 1.55; margin: 0; overflow-wrap: anywhere; }
[data-validator], [data-recovery-permitted] { background: var(--surface-inset); border: 1px solid var(--line); color: var(--text-secondary); font-size: 0.74rem; margin: 0.5rem 0 0; padding: 0.65rem 0.75rem; }
[data-recovery-permitted="false"] { background: var(--status-danger-bg); border-color: var(--status-danger-border); color: var(--status-danger-text); }
ul { display: grid; gap: 0; list-style: none; margin: 0.8rem 0 0; padding: 0; } li { border-top: 1px solid var(--line); padding: 0.7rem 0; } li div { align-items: center; display: flex; justify-content: space-between; } li strong { font-size: 0.78rem; } li p { color: var(--text-secondary); font-size: 0.74rem; margin: 0.35rem 0 0; } li time { color: var(--text-tertiary); display: block; font-size: 0.68rem; margin-top: 0.35rem; }
</style>
