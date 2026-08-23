<script setup lang="ts">
import { CircleAlert, RefreshCw, ShieldAlert } from "lucide-vue-next";
import type { IncidentDetail, RecoveryIntent } from "@agent-py/api-contracts";
import AppBadge from "../../ui/AppBadge.vue";
import AppButton from "../../ui/AppButton.vue";

defineProps<{
  readonly incident: IncidentDetail;
  readonly intent: RecoveryIntent | null;
  readonly stale: boolean;
  readonly errorMessage: string | null;
  readonly actionPending: "approve" | "reject" | "cancel" | null;
}>();
defineEmits<{ approve: []; reject: []; cancel: []; retry: [] }>();

function statusTone(status: RecoveryIntent["status"]): "success" | "danger" | "warning" | "info" | "neutral" {
  if (status === "recovered") return "success";
  if (["denied", "rejected", "verification_failed", "manual_intervention"].includes(status)) return "danger";
  if (status === "awaiting_approval") return "warning";
  if (["queued", "revalidating", "executing", "verifying"].includes(status)) return "info";
  return "neutral";
}
</script>

<template>
  <section class="closure" aria-label="恢复闭环">
    <header><ShieldAlert :size="18" aria-hidden="true" /><div><h3>正式 Recovery Intent</h3><p>以控制平面状态为准，诊断报告不会被推断为恢复成功。</p></div><AppBadge v-if="intent" :tone="statusTone(intent.status)">{{ intent.status }}</AppBadge></header>
    <div v-if="stale" class="closure__notice"><CircleAlert :size="16" aria-hidden="true" /><span>{{ errorMessage ?? "刷新失败，正在显示上次成功状态" }}</span><AppButton size="small" @click="$emit('retry')"><RefreshCw :size="14" aria-hidden="true" />重试</AppButton></div>
    <div v-if="intent" class="closure__body">
      <dl><div><dt>动作类型</dt><dd>{{ intent.action === "restart_compose_service" ? "重启受控 Compose 服务" : "终止 PostgreSQL 阻塞会话" }}</dd></div><div><dt>风险级别</dt><dd>{{ intent.riskTier === "high" ? "高风险" : "低风险" }}</dd></div><div><dt>执行摘要</dt><dd>{{ intent.executionSummary ?? "尚未提供" }}</dd></div></dl>
      <div v-if="intent.action === 'restart_compose_service'" class="closure__automatic"><strong>自动恢复由受控执行器推进</strong><p>页面仅刷新 queued → revalidating → executing → verifying → recovered 的正式状态，不提供通用执行按钮。</p></div>
      <div v-if="intent.action === 'terminate_postgres_blocker' && intent.status === 'awaiting_approval'" class="closure__approval">
        <strong>高风险数据库恢复</strong><p>批准后，服务端会用当前 Incident ID 绑定本次审批；页面不会提交 PID、SQL、目标或执行参数。</p>
        <div><AppButton data-action="approve" variant="danger" :loading="actionPending === 'approve'" @click="$emit('approve')">确认 Incident 并批准</AppButton><AppButton data-action="reject" :loading="actionPending === 'reject'" @click="$emit('reject')">拒绝</AppButton></div>
      </div>
      <div v-if="intent.status === 'verification_failed' || intent.status === 'manual_intervention'" class="closure__blocked"><strong>{{ intent.status === "verification_failed" ? "独立验证未通过" : "需要人工介入" }}</strong><p>保留审计上下文，不自动重试恢复动作。</p></div>
      <div class="closure__checks"><h4>独立验证</h4><p v-if="intent.verification.length === 0">尚无验证里程碑。</p><ul v-else><li v-for="check in intent.verification" :key="check.key"><AppBadge :tone="check.status === 'passed' ? 'success' : check.status === 'failed' ? 'danger' : 'warning'">{{ check.status }}</AppBadge><span>{{ check.safeSummary }}</span></li></ul></div>
    </div>
    <div v-else class="closure__empty"><strong>尚未生成正式 Recovery Intent</strong><p>恢复提案只有通过服务端策略与资格检查后，才会成为可跟踪的正式 Intent。</p></div>
  </section>
</template>

<style scoped>
.closure { padding: 1.1rem; }
header { align-items: flex-start; display: grid; gap: 0.55rem; grid-template-columns: auto minmax(0, 1fr) auto; } header > svg { color: var(--accent); margin-top: 0.08rem; }
h3 { font-size: 0.9rem; margin: 0; } header p { color: var(--text-secondary); font-size: 0.72rem; margin: 0.25rem 0 0; }
.closure__notice { align-items: center; background: var(--status-waiting-bg); border: 1px solid var(--status-waiting-border); color: var(--status-waiting-text); display: flex; gap: 0.55rem; margin-top: 0.9rem; padding: 0.6rem; } .closure__notice span { flex: 1; font-size: 0.74rem; }
dl { margin: 0.9rem 0; } dl div { border-top: 1px solid var(--line); display: grid; gap: 0.75rem; grid-template-columns: 7rem 1fr; padding: 0.65rem 0; } dt { color: var(--text-tertiary); font-size: 0.72rem; } dd { font-size: 0.76rem; margin: 0; overflow-wrap: anywhere; }
.closure__automatic, .closure__approval, .closure__blocked, .closure__empty { border: 1px solid var(--line); padding: 0.85rem; }
.closure__automatic { background: var(--status-running-bg); border-color: var(--status-running-border); }
.closure__approval, .closure__blocked { background: var(--status-danger-bg); border-color: var(--status-danger-border); }
.closure__automatic strong, .closure__approval strong, .closure__blocked strong, .closure__empty strong { font-size: 0.8rem; }
.closure__automatic p, .closure__approval p, .closure__blocked p, .closure__empty p { color: var(--text-secondary); font-size: 0.74rem; line-height: 1.55; margin: 0.35rem 0 0; }
.closure__approval > div { display: flex; flex-wrap: wrap; gap: 0.55rem; margin-top: 0.8rem; }
.closure__checks { border-top: 1px solid var(--line); margin-top: 1rem; padding-top: 0.85rem; } h4 { font-size: 0.8rem; margin: 0; } .closure__checks > p { color: var(--text-tertiary); font-size: 0.74rem; }
ul { display: grid; gap: 0.5rem; list-style: none; margin: 0.7rem 0 0; padding: 0; } li { align-items: center; display: flex; gap: 0.55rem; } li span { color: var(--text-secondary); font-size: 0.74rem; }
</style>
