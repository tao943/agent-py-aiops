<script setup lang="ts">
import { CircleCheck, TriangleAlert } from "lucide-vue-next";
import type { SafeRuntimeConfiguration, SafeRuntimeDependency } from "../../runtime/runtimeStatusClient";
import AppBadge from "../../ui/AppBadge.vue";
defineProps<{ readonly dependencies: readonly SafeRuntimeDependency[]; readonly configuration: readonly SafeRuntimeConfiguration[] }>();
const labels: Record<string, string> = { postgresql: "PostgreSQL", milvus: "Milvus", llm: "LLM", mcp: "MCP", redis: "Redis" };
</script>
<template>
  <section class="dependency-list" aria-label="运行依赖">
    <header><div><span>DEPENDENCIES</span><h2>运行依赖</h2></div><small>{{ dependencies.filter((item) => item.status === 'ready').length }}/{{ dependencies.length }} 可用</small></header>
    <ul><li v-for="item in dependencies" :key="item.name"><CircleCheck v-if="item.status === 'ready'" :size="17" class="ok" /><TriangleAlert v-else :size="17" class="bad" /><span><strong>{{ labels[item.name] ?? item.name }}</strong><small>{{ item.safeSummary }}<template v-if="typeof item.latencyMs === 'number'"> · {{ item.latencyMs.toFixed(0) }}ms</template></small></span><AppBadge :tone="item.status === 'ready' ? 'success' : item.blocking ? 'danger' : 'warning'">{{ item.status === "ready" ? "可用" : item.blocking ? "阻塞" : "非阻塞" }}</AppBadge></li></ul>
    <div class="dependency-list__config"><span v-for="item in configuration" :key="item.name"><i :class="{ invalid: !item.valid }" />{{ labels[item.name] ?? item.name }} {{ item.safeSummary }}</span></div>
  </section>
</template>
<style scoped>
.dependency-list { min-width: 0; }
.dependency-list header { align-items: end; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; padding-bottom: 0.8rem; }
.dependency-list header span { color: var(--accent); font-size: 0.62rem; font-weight: 800; letter-spacing: 0.1em; }.dependency-list h2 { font-size: 0.95rem; margin: 0.15rem 0 0; }.dependency-list header small { color: var(--text-tertiary); font-size: 0.68rem; }
.dependency-list ul { list-style: none; margin: 0; padding: 0; }.dependency-list li { align-items: center; border-bottom: 1px solid var(--line); display: grid; gap: 0.65rem; grid-template-columns: auto minmax(0,1fr) auto; min-height: 4rem; }.dependency-list li > span { display: grid; gap: 0.2rem; }.dependency-list li strong { font-size: 0.77rem; }.dependency-list li small { color: var(--text-tertiary); font-size: 0.67rem; }.ok { color: var(--success); }.bad { color: var(--danger); }
.dependency-list__config { display: flex; flex-wrap: wrap; gap: 0.65rem 1rem; padding-top: 0.8rem; }.dependency-list__config span { align-items: center; color: var(--text-secondary); display: inline-flex; font-size: 0.65rem; gap: 0.3rem; }.dependency-list__config i { background: var(--success); border-radius: 50%; height: 0.4rem; width: 0.4rem; }.dependency-list__config i.invalid { background: var(--danger); }
</style>
