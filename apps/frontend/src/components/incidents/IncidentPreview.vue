<script setup lang="ts">
import { ArrowRight, Bot, ShieldAlert, Stethoscope } from "lucide-vue-next";

import type { IncidentSummary } from "@agent-py/api-contracts";

import AppBadge from "../../ui/AppBadge.vue";
import AppButton from "../../ui/AppButton.vue";

defineProps<{
  readonly incident: IncidentSummary | null;
  readonly diagnosing: boolean;
}>();
const emit = defineEmits<{ diagnose: [incidentId: string]; open: [incidentId: string] }>();

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"
  }).format(new Date(value));
}
</script>

<template>
  <aside class="incident-preview" aria-label="事件预览">
    <div v-if="incident === null" class="incident-preview__empty">选择一个事件查看调查与恢复状态。</div>
    <template v-else>
      <header>
        <div><span>事件预览</span><h2>{{ incident.alertName }}</h2><code>{{ incident.id }}</code></div>
        <AppBadge :tone="incident.status === 'active' ? 'danger' : 'success'">{{ incident.status === 'active' ? '活跃' : '已解决' }}</AppBadge>
      </header>
      <dl>
        <div><dt>影响服务</dt><dd>{{ incident.service ?? '未识别' }}</dd></div>
        <div><dt>环境</dt><dd>{{ incident.environment ?? '未提供' }}</dd></div>
        <div><dt>首次出现</dt><dd>{{ formatTime(incident.firstSeenAt) }}</dd></div>
        <div><dt>最后更新</dt><dd>{{ formatTime(incident.updatedAt) }}</dd></div>
      </dl>
      <section>
        <h3><Bot :size="16" aria-hidden="true" />Agent 状态</h3>
        <p>{{ incident.agentMode ? `${incident.agentMode === 'multi' ? 'Multi' : 'Single'}-Agent` : '尚未确定路由' }} · {{ incident.diagnosticStatus ?? '尚未诊断' }}</p>
      </section>
      <section>
        <h3><ShieldAlert :size="16" aria-hidden="true" />恢复控制面</h3>
        <p v-if="!incident.productionRecoveryExecution">尚未生成正式 Recovery Intent。</p>
        <p v-else>{{ incident.recoveryMode === 'automatic' ? '自动恢复' : '人工复核' }} · {{ incident.recoveryExecutionStatus }}</p>
      </section>
      <footer>
        <AppButton v-if="incident.diagnosticTaskId === null && incident.status === 'active'" variant="primary" :loading="diagnosing" @click="emit('diagnose', incident.id)"><Stethoscope :size="16" aria-hidden="true" />开始诊断</AppButton>
        <AppButton variant="secondary" @click="emit('open', incident.id)">打开调查工作台<ArrowRight :size="16" aria-hidden="true" /></AppButton>
      </footer>
    </template>
  </aside>
</template>

<style scoped>
.incident-preview { background: var(--surface-raised); border-left: 1px solid var(--line); min-height: 0; overflow: auto; padding: 1.2rem; }
.incident-preview__empty { color: var(--text-secondary); font-size: 0.82rem; padding: 2rem 0; text-align: center; }
.incident-preview header { align-items: flex-start; display: flex; gap: 1rem; justify-content: space-between; }
.incident-preview header span { color: var(--accent); font-size: 0.68rem; font-weight: 760; }
.incident-preview h2 { font-size: 1rem; margin: 0.28rem 0; }
.incident-preview code { color: var(--text-tertiary); font-size: 0.68rem; }
.incident-preview dl { border-bottom: 1px solid var(--line); border-top: 1px solid var(--line); display: grid; gap: 0.9rem; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 1.2rem 0; padding: 1rem 0; }
.incident-preview dt { color: var(--text-tertiary); font-size: 0.66rem; }
.incident-preview dd { font-size: 0.78rem; margin: 0.25rem 0 0; }
.incident-preview section { border-bottom: 1px solid var(--line); padding: 0 0 1rem; }
.incident-preview section + section { padding-top: 1rem; }
.incident-preview h3 { align-items: center; display: flex; font-size: 0.78rem; gap: 0.4rem; margin: 0; }
.incident-preview section p { color: var(--text-secondary); font-size: 0.76rem; line-height: 1.5; margin: 0.45rem 0 0; }
.incident-preview footer { display: flex; flex-wrap: wrap; gap: 0.65rem; padding-top: 1.2rem; }
</style>
