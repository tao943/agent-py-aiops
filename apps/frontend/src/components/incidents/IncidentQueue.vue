<script setup lang="ts">
import { ChevronRight, Clock3 } from "lucide-vue-next";

import type { IncidentSummary } from "@agent-py/api-contracts";

import AppBadge from "../../ui/AppBadge.vue";

defineProps<{
  readonly incidents: readonly IncidentSummary[];
  readonly selectedId: string | null;
}>();
const emit = defineEmits<{ select: [incidentId: string]; open: [incidentId: string] }>();

function severityTone(severity: IncidentSummary["severity"]): "danger" | "warning" | "info" | "neutral" {
  if (severity === "critical") return "danger";
  if (severity === "high" || severity === "medium") return "warning";
  if (severity === "low" || severity === "info") return "info";
  return "neutral";
}

function stageLabel(stage: IncidentSummary["currentStage"]): string {
  return ({
    alert: "待调查", investigation: "调查中", decision: "待决策", recovery: "恢复中",
    verification: "验证中", closed: "已关闭"
  } as const)[stage];
}

function elapsed(value: string): string {
  const milliseconds = Math.max(0, Date.now() - new Date(value).getTime());
  const minutes = Math.floor(milliseconds / 60_000);
  if (minutes < 60) return `${Math.max(1, minutes)} 分钟`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} 小时` : `${Math.floor(hours / 24)} 天`;
}
</script>

<template>
  <section class="incident-queue" aria-label="事件队列">
    <p v-if="incidents.length === 0" class="incident-queue__empty">没有符合当前筛选条件的事件。</p>
    <ul v-else>
      <li v-for="incident in incidents" :key="incident.id">
        <button
          type="button"
          class="incident-queue__row"
          :class="{ 'incident-queue__row--selected': incident.id === selectedId }"
          :aria-current="incident.id === selectedId ? 'true' : undefined"
          :aria-label="`选择事件 ${incident.alertName}`"
          @click="emit('select', incident.id)"
        >
          <span class="incident-queue__signal" :data-severity="incident.severity" aria-hidden="true" />
          <span class="incident-queue__main">
            <span class="incident-queue__title"><strong>{{ incident.alertName }}</strong><AppBadge :tone="severityTone(incident.severity)">{{ incident.severity }}</AppBadge></span>
            <span class="incident-queue__meta">{{ incident.service ?? '未知服务' }} · {{ incident.source ?? '未知来源' }}</span>
            <span class="incident-queue__facts"><span><Clock3 :size="13" aria-hidden="true" />持续 {{ elapsed(incident.firstSeenAt) }}</span><span>{{ stageLabel(incident.currentStage) }}</span><span v-if="incident.agentMode">{{ incident.agentMode === 'multi' ? 'Multi-Agent' : 'Single-Agent' }}</span><span>{{ incident.recoveryMode === 'automatic' ? '自动恢复' : incident.recoveryMode === 'manual_review' ? '人工复核' : '未生成恢复' }}</span></span>
          </span>
        </button>
        <button type="button" class="incident-queue__open" :aria-label="`打开事件 ${incident.alertName}`" @click="emit('open', incident.id)"><ChevronRight :size="18" aria-hidden="true" /></button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.incident-queue { background: var(--surface-panel); min-height: 0; overflow: auto; }
.incident-queue ul { list-style: none; margin: 0; padding: 0; }
.incident-queue li { align-items: stretch; border-bottom: 1px solid var(--line); display: grid; grid-template-columns: minmax(0, 1fr) auto; position: relative; }
.incident-queue__row { align-items: stretch; display: grid; grid-template-columns: 3px minmax(0, 1fr); min-height: 7.6rem; padding: 0; text-align: left; width: 100%; }
.incident-queue__row:hover, .incident-queue__row--selected { background: var(--surface-raised); }
.incident-queue__row--selected { box-shadow: inset 0 0 0 1px var(--accent-border); }
.incident-queue__signal { background: var(--info); }
.incident-queue__signal[data-severity="critical"] { background: var(--danger); }
.incident-queue__signal[data-severity="high"], .incident-queue__signal[data-severity="medium"] { background: var(--warning); }
.incident-queue__main { display: grid; gap: 0.48rem; min-width: 0; padding: 0.85rem 2.5rem 0.85rem 0.9rem; }
.incident-queue__title { align-items: center; display: flex; gap: 0.55rem; justify-content: space-between; }
.incident-queue__title strong { font-size: 0.86rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.incident-queue__meta { color: var(--text-secondary); font-size: 0.74rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.incident-queue__facts { color: var(--text-secondary); display: flex; flex-wrap: wrap; font-size: 0.68rem; gap: 0.4rem 0.8rem; }
.incident-queue__facts > span { align-items: center; display: inline-flex; gap: 0.25rem; }
.incident-queue__open { align-items: center; color: var(--text-tertiary); display: inline-flex; justify-content: center; min-width: 2.75rem; }
.incident-queue__open:hover { background: var(--surface-hover); color: var(--accent); }
.incident-queue__empty { color: var(--text-secondary); margin: 0; padding: 2rem 1.2rem; text-align: center; }
</style>
