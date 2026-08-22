<script setup lang="ts">
import type { DiagnosticResultSseEvent } from "@agent-py/api-contracts";

defineProps<{
  readonly diagnostic: DiagnosticResultSseEvent["diagnostic"];
}>();

function displayValue(value: unknown): string {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}
</script>

<template>
  <article class="diagnostic-result-card">
    <header>
      <div>
        <p>诊断结论</p>
        <strong>{{ diagnostic.taskId }}</strong>
      </div>
      <span :class="{ 'diagnostic-result-card__blocked': !diagnostic.executionPermitted }">
        {{ diagnostic.executionPermitted ? "允许执行" : "禁止自动执行" }}
      </span>
    </header>
    <dl>
      <template v-for="(value, key) in diagnostic.rootCause" :key="key">
        <dt>{{ key }}</dt>
        <dd>{{ displayValue(value) }}</dd>
      </template>
      <dt>恢复方式</dt>
      <dd>{{ diagnostic.recoveryMode === "manual_review" ? "人工复核" : diagnostic.recoveryMode }}</dd>
      <dt>核验状态</dt>
      <dd>{{ diagnostic.validatorStatus }}</dd>
      <dt>证据</dt>
      <dd>{{ diagnostic.evidenceIds.length }} 条</dd>
    </dl>
  </article>
</template>

<style scoped>
.diagnostic-result-card { background: var(--surface-raised); border: 1px solid var(--line); border-radius: var(--radius-md); padding: 1rem; }
header { align-items: center; display: flex; gap: 1rem; justify-content: space-between; }
header p { color: var(--text-tertiary); font-size: 0.72rem; margin: 0 0 0.25rem; }
header strong { font-size: 0.88rem; }
header span { background: var(--accent-soft); border-radius: 999px; color: var(--accent-strong); font-size: 0.74rem; font-weight: 700; padding: 0.3rem 0.55rem; }
header span.diagnostic-result-card__blocked { background: #fff0ed; color: #a63d2f; }
dl { display: grid; font-size: 0.8rem; gap: 0.5rem 0.8rem; grid-template-columns: auto minmax(0, 1fr); margin: 0.9rem 0 0; }
dt { color: var(--text-tertiary); }
dd { color: var(--text-secondary); margin: 0; overflow-wrap: anywhere; }
</style>
