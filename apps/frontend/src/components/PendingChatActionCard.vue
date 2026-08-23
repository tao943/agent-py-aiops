<script setup lang="ts">
import { Clock3, ShieldCheck } from "lucide-vue-next";

import type { PendingChatAction } from "@agent-py/api-contracts";

const emit = defineEmits<{
  cancel: [actionId: string];
  confirm: [actionId: string];
}>();
const props = defineProps<{
  readonly action: PendingChatAction;
  readonly isLoading: boolean;
}>();

const isRecoveryApproval = props.action.actionType === "create_recovery_approval";

function expirationLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "有效期未知";
  return `${new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    month: "2-digit",
    day: "2-digit"
  }).format(date)} 前有效`;
}
</script>

<template>
  <article class="pending-action" aria-labelledby="pending-action-title">
    <div class="pending-action__icon" aria-hidden="true"><ShieldCheck :size="18" /></div>
    <div class="pending-action__body">
      <header>
        <div>
          <p>需要你的确认</p>
          <h3 id="pending-action-title">
            {{ isRecoveryApproval ? "创建人工审批请求" : "启动故障诊断" }}
          </h3>
        </div>
        <span>{{ action.status === "confirmed" ? "已进入队列" : "待确认" }}</span>
      </header>
      <p class="pending-action__boundary">
        {{
          isRecoveryApproval
            ? "此操作只创建人工审批请求，不会批准或执行恢复。"
            : "此操作会启动或复用诊断任务，不会执行恢复动作。"
        }}
      </p>
      <dl>
        <div><dt>目标</dt><dd>{{ action.targetResourceId }}</dd></div>
        <div><dt><Clock3 :size="13" aria-hidden="true" />有效期</dt><dd>{{ expirationLabel(action.expiresAt) }}</dd></div>
      </dl>
      <div v-if="action.status === 'pending'" class="pending-action__commands">
        <button
          data-action="cancel"
          type="button"
          :disabled="isLoading"
          @click="emit('cancel', action.id)"
        >取消</button>
        <button
          class="pending-action__confirm"
          data-action="confirm"
          type="button"
          :disabled="isLoading"
          @click="emit('confirm', action.id)"
        >{{ isLoading ? "正在提交" : "确认并加入队列" }}</button>
      </div>
    </div>
  </article>
</template>

<style scoped>
.pending-action { align-items: start; background: var(--surface-raised); border: 1px solid var(--accent-border); border-radius: var(--radius-md); display: grid; gap: 0.8rem; grid-template-columns: auto minmax(0, 1fr); max-width: 46rem; padding: 1rem; }
.pending-action__icon { align-items: center; background: var(--accent-soft); border-radius: 50%; color: var(--accent-strong); display: inline-flex; height: 2rem; justify-content: center; width: 2rem; }
.pending-action__body { min-width: 0; }
header { align-items: start; display: flex; gap: 1rem; justify-content: space-between; }
header p { color: var(--text-tertiary); font-size: 0.72rem; margin: 0 0 0.2rem; }
h3 { font-size: 0.9rem; margin: 0; text-wrap: balance; }
header span { background: var(--accent-soft); border-radius: 999px; color: var(--accent-strong); flex: none; font-size: 0.72rem; font-weight: 700; padding: 0.28rem 0.5rem; }
.pending-action__boundary { color: var(--text-secondary); font-size: 0.8rem; line-height: 1.6; margin: 0.65rem 0; max-width: 68ch; }
dl { display: flex; flex-wrap: wrap; gap: 0.35rem 1rem; margin: 0; }
dl div { align-items: baseline; display: flex; gap: 0.4rem; min-width: 0; }
dt { align-items: center; color: var(--text-tertiary); display: inline-flex; font-size: 0.72rem; gap: 0.25rem; }
dd { color: var(--text-secondary); font-size: 0.76rem; margin: 0; overflow-wrap: anywhere; }
.pending-action__commands { display: flex; flex-wrap: wrap; gap: 0.5rem; justify-content: flex-end; margin-top: 0.85rem; }
button { border: 1px solid var(--line-strong); border-radius: var(--radius-sm); font-size: 0.78rem; font-weight: 650; min-height: 2.25rem; padding: 0 0.7rem; }
button:hover:not(:disabled) { background: var(--surface-hover); }
button:disabled { opacity: 0.58; }
button.pending-action__confirm { background: var(--accent); border-color: var(--accent); color: #fff; }
button.pending-action__confirm:hover:not(:disabled) { background: var(--accent-strong); }
@media (max-width: 640px) { .pending-action { grid-template-columns: minmax(0, 1fr); } .pending-action__icon { display: none; } header { align-items: flex-start; } .pending-action__commands { justify-content: stretch; } .pending-action__commands button { flex: 1 1 9rem; } }
</style>
