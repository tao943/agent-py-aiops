<script setup lang="ts">
defineProps<{
  readonly metrics: {
    readonly activeCount: number;
    readonly criticalCount: number;
    readonly pendingApprovalCount: number;
    readonly automaticRecoveryCount: number;
    readonly hasProductionRecovery: boolean;
  };
}>();
</script>

<template>
  <section class="incident-metrics" aria-label="事件指标">
    <article><span>活跃事件</span><strong>{{ metrics.activeCount }}</strong><small>当前列表</small></article>
    <article><span>严重事件</span><strong class="incident-metrics__danger">{{ metrics.criticalCount }}</strong><small>Critical</small></article>
    <article><span>等待人工批准</span><strong class="incident-metrics__warning">{{ metrics.pendingApprovalCount }}</strong><small>受策略门保护</small></article>
    <article><span>自动恢复执行中</span><strong>{{ metrics.hasProductionRecovery ? metrics.automaticRecoveryCount : '未启用' }}</strong><small>仅正式控制面</small></article>
    <article><span>24 小时安全闭环率</span><strong>暂无数据</strong><small>等待持久指标</small></article>
  </section>
</template>

<style scoped>
.incident-metrics { display: grid; gap: 1px; grid-template-columns: repeat(5, minmax(0, 1fr)); overflow: hidden; }
.incident-metrics article { background: var(--surface-raised); border-right: 1px solid var(--line); display: grid; gap: 0.28rem; min-height: 6.5rem; padding: 1rem 1.1rem; }
.incident-metrics span, .incident-metrics small { color: var(--text-secondary); font-size: 0.7rem; }
.incident-metrics strong { font-size: clamp(1.15rem, 2vw, 1.6rem); font-variant-numeric: tabular-nums; }
.incident-metrics__danger { color: var(--danger); }
.incident-metrics__warning { color: var(--warning); }
@media (max-width: 1100px) { .incident-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 640px) { .incident-metrics { display: flex; overflow-x: auto; } .incident-metrics article { min-width: 10rem; } }
</style>
