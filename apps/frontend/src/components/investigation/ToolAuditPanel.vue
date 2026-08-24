<script setup lang="ts">
import { Clock3, Wrench } from "lucide-vue-next";
import type { AiopsDiagnosticEvidenceChain } from "@agent-py/api-contracts";
import AppBadge from "../../ui/AppBadge.vue";
defineProps<{ readonly chain: AiopsDiagnosticEvidenceChain | null }>();
</script>

<template>
  <section class="tool-audit" aria-label="工具审计">
    <header><Wrench :size="17" aria-hidden="true" /><div><h3>白名单工具调用</h3><p>仅展示工具名、状态、耗时和安全摘要；参数与完整输出不在前端暴露。</p></div></header>
    <div v-if="chain?.toolCalls.length" class="tool-audit__table">
      <div class="tool-audit__row tool-audit__row--head"><span>工具</span><span>状态</span><span>耗时</span><span>公开摘要</span></div>
      <div v-for="tool in chain.toolCalls" :key="tool.id" class="tool-audit__row">
        <strong>{{ tool.toolName }}</strong><AppBadge>{{ tool.status }}</AppBadge><span><Clock3 :size="13" aria-hidden="true" />{{ tool.durationMs === null ? "未提供" : `${tool.durationMs} ms` }}</span><p>{{ tool.resultSummary ?? "未提供安全摘要" }}</p>
      </div>
    </div>
    <p v-else class="empty">本次调查尚无工具调用记录。</p>
  </section>
</template>

<style scoped>
.tool-audit { padding: 1.1rem; }
header { align-items: flex-start; display: flex; gap: 0.55rem; } header > svg { color: var(--accent); margin-top: 0.08rem; }
h3 { font-size: 0.9rem; margin: 0; } header p, .empty { color: var(--text-secondary); font-size: 0.72rem; margin: 0.25rem 0 0; }
.tool-audit__table { border: 1px solid var(--line); margin-top: 1rem; overflow-x: auto; }
.tool-audit__row { align-items: center; display: grid; gap: 0.8rem; grid-template-columns: minmax(8rem, 0.7fr) 7rem 7rem minmax(14rem, 1.3fr); min-width: 42rem; padding: 0.65rem 0.75rem; }
.tool-audit__row:not(:last-child) { border-bottom: 1px solid var(--line); }
.tool-audit__row--head { background: var(--surface-inset); color: var(--text-tertiary); font-size: 0.7rem; font-weight: 700; }
strong, span, p { font-size: 0.74rem; } .tool-audit__row > span { align-items: center; color: var(--text-secondary); display: flex; gap: 0.3rem; } .tool-audit__row > p { color: var(--text-secondary); line-height: 1.45; margin: 0; overflow-wrap: anywhere; }
</style>
