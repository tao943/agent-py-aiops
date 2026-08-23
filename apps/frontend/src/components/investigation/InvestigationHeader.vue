<script setup lang="ts">
import { ArrowLeft, RefreshCw } from "lucide-vue-next";
import type { IncidentDetail } from "@agent-py/api-contracts";
import AppBadge from "../../ui/AppBadge.vue";
import AppButton from "../../ui/AppButton.vue";

defineProps<{ readonly incident: IncidentDetail; readonly refreshing: boolean; readonly stale: boolean; readonly diagnosticRunning: boolean }>();
defineEmits<{ back: []; refresh: []; cancelDiagnostic: [] }>();

function severityTone(severity: IncidentDetail["severity"]): "danger" | "warning" | "neutral" {
  if (severity === "critical" || severity === "high") return "danger";
  if (severity === "medium") return "warning";
  return "neutral";
}
</script>

<template>
  <header class="investigation-header">
    <AppButton size="small" variant="quiet" aria-label="返回事件中心" @click="$emit('back')"><ArrowLeft :size="16" aria-hidden="true" /></AppButton>
    <div class="investigation-header__identity">
      <div><AppBadge :tone="severityTone(incident.severity)">{{ incident.severity }}</AppBadge><span>{{ incident.service ?? "未知服务" }}</span><span>{{ incident.environment ?? "环境未提供" }}</span></div>
      <h2>{{ incident.alertName }}</h2>
      <p><code>{{ incident.id }}</code><span v-if="stale">恢复状态刷新失败，当前展示上次成功结果</span></p>
    </div>
    <div class="investigation-header__actions">
      <AppBadge :tone="incident.status === 'resolved' ? 'success' : 'warning'">{{ incident.status === "resolved" ? "已解决" : "处理中" }}</AppBadge>
      <AppButton v-if="diagnosticRunning" size="small" variant="danger" @click="$emit('cancelDiagnostic')">取消诊断</AppButton>
      <AppButton size="small" :loading="refreshing" @click="$emit('refresh')"><RefreshCw :size="15" aria-hidden="true" />刷新</AppButton>
    </div>
  </header>
</template>

<style scoped>
.investigation-header { align-items: flex-start; background: var(--surface-panel); border-bottom: 1px solid var(--line); display: grid; gap: 0.8rem; grid-template-columns: auto minmax(0, 1fr) auto; padding: 0.9rem 1.1rem; }
.investigation-header__identity { min-width: 0; }
.investigation-header__identity > div { align-items: center; color: var(--text-secondary); display: flex; flex-wrap: wrap; font-size: 0.72rem; gap: 0.55rem; }
h2 { font-size: 1.05rem; margin: 0.32rem 0 0.2rem; overflow-wrap: anywhere; text-wrap: balance; }
p { align-items: center; color: var(--text-tertiary); display: flex; flex-wrap: wrap; font-size: 0.7rem; gap: 0.65rem; margin: 0; }
p span { color: var(--warning); }
code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.investigation-header__actions { align-items: center; display: flex; gap: 0.6rem; }
@media (max-width: 700px) { .investigation-header { grid-template-columns: auto minmax(0, 1fr); } .investigation-header__actions { grid-column: 2; justify-content: flex-start; } }
</style>
