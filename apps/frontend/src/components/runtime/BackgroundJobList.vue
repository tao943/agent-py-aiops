<script setup lang="ts">
import type { BackgroundJob } from "@agent-py/api-contracts";
import AppBadge from "../../ui/AppBadge.vue";
defineProps<{ readonly jobs: readonly BackgroundJob[] }>();
function tone(status: BackgroundJob["status"]): "neutral" | "info" | "success" | "danger" { return status === "failed" ? "danger" : status === "succeeded" ? "success" : status === "running" || status === "queued" ? "info" : "neutral"; }
const labels: Record<string, string> = { queued: "排队中", running: "运行中", succeeded: "已完成", failed: "任务执行失败", cancelled: "已取消" };
</script>
<template>
  <section class="job-list" aria-label="后台任务">
    <header><span>BACKGROUND JOBS</span><h2>最近任务</h2></header>
    <div v-if="jobs.length === 0" class="job-list__empty">暂无后台任务。</div>
    <article v-for="job in jobs.slice(0, 8)" :key="job.id"><div><strong>{{ job.kind }}</strong><small>{{ job.resourceType }} · 尝试 {{ job.attempt }}/{{ job.maxAttempts }}</small></div><AppBadge :tone="tone(job.status)">{{ labels[job.status] ?? job.status }}</AppBadge></article>
  </section>
</template>
<style scoped>
.job-list header { border-bottom: 1px solid var(--line); padding-bottom: 0.8rem; }.job-list header span { color: var(--accent); font-size: 0.62rem; font-weight: 800; letter-spacing: 0.1em; }.job-list h2 { font-size: 0.95rem; margin: 0.15rem 0 0; }.job-list article { align-items: center; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; min-height: 3.8rem; }.job-list article div { display: grid; gap: 0.2rem; }.job-list strong { font-size: 0.75rem; }.job-list small, .job-list__empty { color: var(--text-tertiary); font-size: 0.66rem; }.job-list__empty { padding: 1rem 0; }
</style>
