<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import AiopsReportPanel from "../components/AiopsReportPanel.vue";
import AppErrorState from "../components/AppErrorState.vue";
import ExecutionTrace from "../components/investigation/ExecutionTrace.vue";
import HypothesisEvidencePanel from "../components/investigation/HypothesisEvidencePanel.vue";
import InvestigationContextAside from "../components/investigation/InvestigationContextAside.vue";
import InvestigationHeader from "../components/investigation/InvestigationHeader.vue";
import RecoveryClosurePanel from "../components/investigation/RecoveryClosurePanel.vue";
import ToolAuditPanel from "../components/investigation/ToolAuditPanel.vue";
import { toPublicInvestigationResult, useAiopsStore } from "../stores/aiops";
import { useIncidentStore } from "../stores/incidents";
import { useRecoveryStore } from "../stores/recovery";
import AppSkeleton from "../ui/AppSkeleton.vue";
import AppTabs from "../ui/AppTabs.vue";

const route = useRoute();
const router = useRouter();
const incidents = useIncidentStore();
const recovery = useRecoveryStore();
const aiops = useAiopsStore();
const activeTab = ref("overview");
const incidentId = computed(() => String(route.params.incidentId ?? ""));
const incident = computed(() => incidents.detail?.id === incidentId.value ? incidents.detail : null);
const chain = computed(() => incident.value?.evidenceChain ?? null);
const result = computed(() => toPublicInvestigationResult(chain.value));
const report = computed(() => {
  const item = chain.value?.reports.at(-1) ?? null;
  return item === null ? null : { id: item.id, title: item.title, content: item.content, createdAt: item.createdAt };
});

const tabs = [
  { id: "overview", label: "概览" },
  { id: "trace", label: "执行链" },
  { id: "hypothesis", label: "假设与证据" },
  { id: "tools", label: "工具审计" },
  { id: "recovery", label: "恢复闭环" },
  { id: "audit", label: "审计时间线" }
] as const;

async function load(): Promise<void> {
  const loaded = await incidents.loadDetail(incidentId.value);
  if (loaded.recoveryIntentId !== null) {
    if (recovery.intentId === loaded.recoveryIntentId) await recovery.refresh();
    else await recovery.start(loaded.recoveryIntentId, loaded.recoveryIntent, loaded.recoveryEvents);
  } else {
    recovery.stop();
  }
  if (
    loaded.diagnosticTaskId !== null &&
    (loaded.diagnosticStatus === "accepted" || loaded.diagnosticStatus === "running") &&
    !aiops.isRunning
  ) {
    void aiops.resumeDiagnostic(loaded.diagnosticTaskId)
      .then(() => incidents.loadDetail(loaded.id))
      .catch(() => undefined);
  }
}

function refresh(): void { void load().catch(() => undefined); }
function approve(): void { if (incident.value) void recovery.approve(incident.value.id).catch(() => undefined); }
function reject(): void { void recovery.reject().catch(() => undefined); }
function cancel(): void { void recovery.cancel().catch(() => undefined); }
function cancelDiagnostic(): void { void aiops.cancelActive().then(refresh).catch(() => undefined); }

watch(incidentId, refresh);
onMounted(refresh);
onUnmounted(() => recovery.stop());
</script>

<template>
  <section class="workspace" aria-label="调查工作台">
    <AppSkeleton v-if="incidents.isDetailLoading && incident === null" class="workspace__loading" label="正在加载调查工作台" :lines="8" />
    <AppErrorState v-else-if="incidents.detailErrorMessage !== null && incident === null" :can-retry="true" :message="incidents.detailErrorMessage" @retry="refresh" />
    <template v-else-if="incident">
      <InvestigationHeader :incident="incident" :refreshing="incidents.isDetailLoading || recovery.isRefreshing" :stale="recovery.stale" :diagnostic-running="aiops.isRunning || incident.diagnosticStatus === 'running'" @back="router.push('/incidents')" @refresh="refresh" @cancel-diagnostic="cancelDiagnostic" />
      <div class="workspace__tabs"><AppTabs v-model="activeTab" label="调查工作台视图" :items="tabs" /></div>
      <div class="workspace__body">
        <main :id="`panel-${activeTab}`" class="workspace__panel" role="tabpanel" :aria-labelledby="`tab-${activeTab}`">
          <template v-if="activeTab === 'overview'">
            <section class="overview" aria-label="事件概览">
              <div><span>告警摘要</span><h3>{{ incident.summary ?? "未提供告警摘要" }}</h3><p>首次出现 {{ new Date(incident.firstSeenAt).toLocaleString('zh-CN') }} · 已投递 {{ incident.deliveryCount }} 次</p></div>
              <div class="overview__state">
                <p :data-validator="result.validatorOrigin">{{ result.validatorMessage }}</p>
                <p :data-recovery-permitted="String(result.executionPermitted)">{{ result.executionPermitted === false ? "禁止自动执行" : result.executionPermitted === true ? "允许进入受控恢复" : "执行权限未提供" }}</p>
                <ul v-if="result.agentMode === 'multi'"><li v-for="item in result.specialists" :key="item.role" :data-status="item.status"><strong>{{ item.role }}</strong><span>{{ item.safeSummary }}</span></li></ul>
              </div>
            </section>
            <AiopsReportPanel :report="report" :is-running="incident.diagnosticStatus === 'running'" :has-task="incident.diagnosticTaskId !== null" :task-failed="incident.diagnosticStatus === 'failed'" />
          </template>
          <ExecutionTrace v-else-if="activeTab === 'trace'" :chain="chain" />
          <HypothesisEvidencePanel v-else-if="activeTab === 'hypothesis'" :chain="chain" :result="result" />
          <ToolAuditPanel v-else-if="activeTab === 'tools'" :chain="chain" />
          <RecoveryClosurePanel v-else-if="activeTab === 'recovery'" :incident="incident" :intent="recovery.intent ?? incident.recoveryIntent" :stale="recovery.stale" :error-message="recovery.errorMessage" :action-pending="recovery.actionPending" @approve="approve" @reject="reject" @cancel="cancel" @retry="recovery.refresh" />
          <section v-else class="audit" aria-label="审计时间线">
            <header><h3>恢复控制平面事件</h3><p>按 durable sequence 展示追加式公开事件。</p></header>
            <ol v-if="recovery.events.length"><li v-for="event in recovery.events" :key="event.sequence"><span>{{ event.sequence }}</span><div><strong>{{ event.safeSummary }}</strong><p>{{ event.fromStatus ?? "开始" }} → {{ event.toStatus }}</p><time :datetime="event.createdAt">{{ new Date(event.createdAt).toLocaleString('zh-CN') }}</time></div></li></ol>
            <p v-else class="audit__empty">当前没有正式恢复审计事件。</p>
          </section>
        </main>
        <InvestigationContextAside :incident="incident" :result="result" />
      </div>
    </template>
  </section>
</template>

<style scoped>
.workspace { background: var(--surface-canvas); display: grid; grid-template-rows: auto auto minmax(0, 1fr); height: 100%; min-height: 0; }
.workspace__tabs { background: var(--surface-panel); overflow-x: auto; padding: 0 1.1rem; }
.workspace__tabs :deep(.app-tabs) { min-width: 38rem; }
.workspace__body { display: grid; grid-template-columns: minmax(0, 1fr) 17rem; min-height: 0; }
.workspace__panel { background: var(--surface-panel); min-height: 0; min-width: 0; overflow-y: auto; }
.workspace__loading, .workspace > :deep(.error-state) { margin: 1rem; }
.overview { border-bottom: 1px solid var(--line); display: grid; gap: 1rem; grid-template-columns: minmax(0, 1fr) minmax(18rem, 0.8fr); padding: 1.1rem; }
.overview > div:first-child > span { color: var(--accent); font-size: 0.7rem; font-weight: 750; }
.overview h3 { font-size: 0.95rem; line-height: 1.5; margin: 0.35rem 0; text-wrap: pretty; }
.overview p { color: var(--text-secondary); font-size: 0.72rem; margin: 0; }
.overview__state { display: grid; gap: 0.45rem; }
.overview__state > p { background: var(--surface-inset); border: 1px solid var(--line); padding: 0.55rem 0.65rem; }
.overview__state > [data-recovery-permitted="false"] { background: var(--status-danger-bg); border-color: var(--status-danger-border); color: var(--status-danger-text); }
.overview__state ul { list-style: none; margin: 0; padding: 0; }
.overview__state li { border-top: 1px solid var(--line); display: grid; gap: 0.7rem; grid-template-columns: 5rem 1fr; padding: 0.45rem 0; }
.overview__state li strong, .overview__state li span { font-size: 0.7rem; } .overview__state li span { color: var(--text-secondary); }
.audit { padding: 1.1rem; } .audit header h3 { font-size: 0.9rem; margin: 0; } .audit header p, .audit__empty { color: var(--text-secondary); font-size: 0.72rem; margin: 0.25rem 0 0; }
.audit ol { list-style: none; margin: 1rem 0 0; padding: 0; } .audit li { display: grid; gap: 0.7rem; grid-template-columns: 2rem 1fr; padding: 0.65rem 0; } .audit li:not(:last-child) { border-bottom: 1px solid var(--line); }
.audit li > span { align-items: center; background: var(--surface-inset); border: 1px solid var(--line); border-radius: 50%; display: flex; font-size: 0.68rem; height: 2rem; justify-content: center; }
.audit li strong { font-size: 0.76rem; } .audit li p, .audit li time { color: var(--text-tertiary); display: block; font-size: 0.68rem; margin: 0.2rem 0 0; }
@media (max-width: 980px) { .workspace { height: auto; min-height: 100%; } .workspace__body { grid-template-columns: 1fr; } .workspace__panel { overflow: visible; } }
@media (max-width: 700px) { .overview { grid-template-columns: 1fr; } }
</style>
