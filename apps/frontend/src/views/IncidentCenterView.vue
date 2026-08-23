<script setup lang="ts">
import { Filter, RefreshCw } from "lucide-vue-next";
import { onMounted, onUnmounted, ref } from "vue";
import { useRouter } from "vue-router";

import IncidentMetrics from "../components/incidents/IncidentMetrics.vue";
import IncidentPreview from "../components/incidents/IncidentPreview.vue";
import IncidentQueue from "../components/incidents/IncidentQueue.vue";
import AppErrorState from "../components/AppErrorState.vue";
import { useIncidentStore } from "../stores/incidents";
import AppButton from "../ui/AppButton.vue";
import AppDrawer from "../ui/AppDrawer.vue";
import AppSkeleton from "../ui/AppSkeleton.vue";

const incidents = useIncidentStore();
const router = useRouter();
const isNarrow = ref(false);
const drawerOpen = ref(false);
let media: MediaQueryList | null = null;

function refresh(): void {
  void incidents.initialize().catch(() => undefined);
}

function selectIncident(incidentId: string): void {
  incidents.select(incidentId);
  if (isNarrow.value) drawerOpen.value = true;
}

function openIncident(incidentId: string): void {
  void router.push({ name: "incident-workspace", params: { incidentId } });
}

function diagnose(incidentId: string): void {
  void incidents.startDiagnostic(incidentId).catch(() => undefined);
}

function readSelect(event: Event): string {
  return (event.target as HTMLSelectElement).value;
}

function updateMedia(event: MediaQueryListEvent | MediaQueryList): void {
  isNarrow.value = event.matches;
  if (!event.matches) drawerOpen.value = false;
}

onMounted(() => {
  media = window.matchMedia?.("(max-width: 900px)") ?? null;
  if (media !== null) {
    updateMedia(media);
    media.addEventListener("change", updateMedia);
  }
  refresh();
});

onUnmounted(() => media?.removeEventListener("change", updateMedia));
</script>

<template>
  <section class="incident-center" aria-label="事件中心">
    <header class="incident-center__header">
      <div><span>事件中心</span><h2>告警、调查与恢复闭环</h2><p>只展示当前账号可访问的真实 Incident 投影。</p></div>
      <AppButton size="small" :loading="incidents.isLoading" @click="refresh"><RefreshCw :size="15" aria-hidden="true" />刷新</AppButton>
    </header>

    <IncidentMetrics :metrics="incidents.metrics" />

    <div class="incident-center__toolbar" aria-label="事件筛选">
      <Filter :size="16" aria-hidden="true" />
      <label>状态<select :value="incidents.statusFilter" @change="incidents.setStatusFilter(readSelect($event) as 'all' | 'active' | 'resolved')"><option value="all">全部</option><option value="active">活跃</option><option value="resolved">已解决</option></select></label>
      <label>级别<select :value="incidents.severityFilter" @change="incidents.setSeverityFilter(readSelect($event) as typeof incidents.severityFilter)"><option value="all">全部</option><option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option><option value="info">Info</option><option value="unknown">Unknown</option></select></label>
      <span v-if="incidents.stale" class="incident-center__stale">数据刷新失败，正在显示上次结果</span>
    </div>

    <AppSkeleton v-if="incidents.isLoading && incidents.items.length === 0" class="incident-center__loading" label="正在加载事件队列" :lines="6" />
    <AppErrorState v-else-if="incidents.errorMessage !== null && incidents.items.length === 0" :can-retry="true" :message="incidents.errorMessage" @retry="refresh" />
    <div v-else class="incident-center__workspace">
      <div class="incident-center__queue">
        <IncidentQueue :incidents="incidents.visibleIncidents" :selected-id="incidents.selectedId" @open="openIncident" @select="selectIncident" />
        <AppButton v-if="incidents.nextCursor !== null" class="incident-center__more" size="small" :loading="incidents.isLoadingMore" @click="incidents.loadMore">加载更多</AppButton>
      </div>
      <IncidentPreview class="incident-center__preview" :diagnosing="incidents.selectedId !== null && incidents.diagnosingIds.includes(incidents.selectedId)" :incident="incidents.selectedIncident" @diagnose="diagnose" @open="openIncident" />
    </div>

    <AppDrawer :open="drawerOpen" title="事件预览" @close="drawerOpen = false">
      <IncidentPreview :diagnosing="incidents.selectedId !== null && incidents.diagnosingIds.includes(incidents.selectedId)" :incident="incidents.selectedIncident" @diagnose="diagnose" @open="openIncident" />
    </AppDrawer>
  </section>
</template>

<style scoped>
.incident-center { background: var(--surface-canvas); display: grid; grid-template-rows: auto auto auto minmax(0, 1fr); height: 100%; min-height: 0; }
.incident-center__header { align-items: center; background: var(--surface-panel); border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; padding: 1rem clamp(1rem, 2.5vw, 1.75rem); }
.incident-center__header span { color: var(--accent); font-size: 0.68rem; font-weight: 780; letter-spacing: 0.06em; }
.incident-center__header h2 { font-size: 1.05rem; margin: 0.2rem 0; }
.incident-center__header p { color: var(--text-secondary); font-size: 0.72rem; margin: 0; }
.incident-center__toolbar { align-items: center; background: var(--surface-panel); border-bottom: 1px solid var(--line); border-top: 1px solid var(--line); display: flex; gap: 0.8rem; min-height: 3.4rem; padding: 0.55rem 1rem; }
.incident-center__toolbar label { align-items: center; color: var(--text-secondary); display: inline-flex; font-size: 0.72rem; gap: 0.35rem; }
.incident-center__toolbar select { background: var(--surface-raised); border: 1px solid var(--line-strong); border-radius: var(--radius-control); min-height: 2.25rem; padding: 0 1.8rem 0 0.55rem; }
.incident-center__stale { color: var(--warning); font-size: 0.72rem; margin-left: auto; }
.incident-center__workspace { display: grid; grid-template-columns: minmax(24rem, 1.3fr) minmax(20rem, 0.7fr); min-height: 0; }
.incident-center__queue { min-height: 0; overflow: auto; }
.incident-center__more { margin: 0.8rem auto; }
.incident-center__loading, .incident-center > :deep(.error-state) { margin: 1rem; }
@media (max-width: 900px) { .incident-center { height: auto; min-height: 100%; } .incident-center__workspace { display: block; } .incident-center__preview { display: none; } .incident-center__toolbar { align-items: flex-start; flex-wrap: wrap; } .incident-center__stale { margin-left: 0; width: 100%; } }
</style>
