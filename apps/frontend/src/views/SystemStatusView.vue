<script setup lang="ts">
import { Activity, CircleCheck, RefreshCw, TriangleAlert } from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted } from "vue";
import AppErrorState from "../components/AppErrorState.vue";
import BackgroundJobList from "../components/runtime/BackgroundJobList.vue";
import DependencyStatusList from "../components/runtime/DependencyStatusList.vue";
import EvaluationSummary from "../components/runtime/EvaluationSummary.vue";
import { useRuntimeStatusStore } from "../stores/runtimeStatus";
import AppBadge from "../ui/AppBadge.vue";
import AppButton from "../ui/AppButton.vue";
import AppSkeleton from "../ui/AppSkeleton.vue";

const runtime = useRuntimeStatusStore();
const blockingReady = computed(() => runtime.snapshot?.dependencies.filter((item) => item.blocking).every((item) => item.status === "ready") ?? false);
onMounted(() => { void runtime.refresh().catch(() => undefined); runtime.startPolling(); });
onBeforeUnmount(runtime.stopPolling);
function refresh(): void { void runtime.refresh().catch(() => undefined); }
</script>
<template>
  <section class="system-status" aria-label="系统状态">
    <header class="system-status__header"><div><span>RUNTIME OBSERVABILITY</span><h1>系统状态</h1><p>区分进程存活、依赖就绪、配置有效性与异步任务状态。</p></div><div><AppBadge v-if="runtime.stale" tone="warning">数据已陈旧</AppBadge><AppButton size="small" :loading="runtime.loading" @click="refresh"><RefreshCw v-if="!runtime.loading" :size="15" />刷新</AppButton></div></header>
    <AppSkeleton v-if="runtime.loading && runtime.snapshot === null" label="正在读取完整运行状态" />
    <AppErrorState v-else-if="runtime.errorMessage && runtime.snapshot === null" :message="runtime.errorMessage" :can-retry="true" @retry="refresh" />
    <div v-else-if="runtime.snapshot" class="system-status__content">
      <section class="system-status__overview">
        <article data-capability="api-process"><CircleCheck :size="20" /><span><small>API PROCESS</small><strong>进程在线</strong><em>{{ runtime.snapshot.process.service }} · {{ runtime.snapshot.process.version }}</em></span></article>
        <article data-capability="full-runtime" :class="{ degraded: runtime.snapshot.readinessStatus === 'degraded' }"><Activity v-if="blockingReady" :size="20" /><TriangleAlert v-else :size="20" /><span><small>FULL RUNTIME</small><strong>{{ runtime.snapshot.readinessStatus === "ready" ? "依赖就绪" : "依赖降级" }}</strong><em>{{ blockingReady ? "阻塞依赖可用" : "至少一个阻塞依赖不可用" }}</em></span></article>
        <article><RefreshCw :size="20" /><span><small>LAST CHECK</small><strong>{{ new Date(runtime.snapshot.checkedAt).toLocaleTimeString("zh-CN", { hour12: false }) }}</strong><em>每 30 秒刷新，页面隐藏时暂停</em></span></article>
      </section>
      <div class="system-status__grid"><DependencyStatusList :dependencies="runtime.snapshot.dependencies" :configuration="runtime.snapshot.configuration" /><div class="system-status__secondary"><BackgroundJobList :jobs="runtime.snapshot.jobs" /><EvaluationSummary :items="runtime.snapshot.evaluations" /></div></div>
    </div>
  </section>
</template>
<style scoped>
.system-status { background: var(--surface-canvas); height: 100%; overflow-y: auto; padding: clamp(1rem, 3vw, 2rem); }
.system-status__header, .system-status__header > div:last-child { align-items: center; display: flex; justify-content: space-between; }.system-status__header { gap: 1rem; margin-bottom: 1.2rem; }.system-status__header > div:last-child { gap: 0.6rem; }.system-status__header > div:first-child > span { color: var(--accent); font-size: 0.62rem; font-weight: 800; letter-spacing: 0.11em; }.system-status h1 { font-size: 1.15rem; margin: 0.15rem 0; }.system-status__header p { color: var(--text-secondary); font-size: 0.72rem; margin: 0; }
.system-status__overview { background: var(--surface-raised); border: 1px solid var(--line); border-radius: var(--radius-panel); display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }.system-status__overview article { align-items: flex-start; color: var(--success); display: grid; gap: 0.7rem; grid-template-columns: auto minmax(0,1fr); min-height: 7rem; padding: 1.2rem; }.system-status__overview article + article { border-left: 1px solid var(--line); }.system-status__overview article.degraded { color: var(--warning); }.system-status__overview article span { display: grid; gap: 0.2rem; }.system-status__overview small { color: var(--text-tertiary); font-size: 0.61rem; font-style: normal; font-weight: 760; letter-spacing: 0.08em; }.system-status__overview strong { color: var(--text-primary); font-size: 0.88rem; }.system-status__overview em { color: var(--text-secondary); font-size: 0.68rem; font-style: normal; }
.system-status__grid { background: var(--surface-raised); border: 1px solid var(--line); border-radius: var(--radius-panel); display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(20rem, 0.8fr); margin-top: 1rem; }.system-status__grid > :first-child { border-right: 1px solid var(--line); padding: 1.2rem; }.system-status__secondary { display: grid; grid-template-rows: auto auto; }.system-status__secondary > * { padding: 1.2rem; }.system-status__secondary > * + * { border-top: 1px solid var(--line); }
@media (max-width: 900px) { .system-status__overview, .system-status__grid { grid-template-columns: 1fr; }.system-status__overview article + article { border-left: 0; border-top: 1px solid var(--line); }.system-status__grid > :first-child { border-bottom: 1px solid var(--line); border-right: 0; } }
@media (max-width: 600px) { .system-status__header { align-items: flex-start; flex-direction: column; } }
</style>
