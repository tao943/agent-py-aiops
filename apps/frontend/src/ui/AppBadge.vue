<script setup lang="ts">
withDefaults(defineProps<{
  readonly tone?: "neutral" | "info" | "success" | "warning" | "danger";
}>(), { tone: "neutral" });
</script>

<template>
  <span class="app-badge" :class="`app-badge--${tone}`" :data-tone="tone"><slot /></span>
</template>

<style scoped>
.app-badge { align-items: center; border: 1px solid var(--status-neutral-border); border-radius: 999px; color: var(--status-neutral-text); display: inline-flex; font-size: 0.72rem; font-weight: 700; line-height: 1; min-height: 1.6rem; padding: 0.2rem 0.55rem; }
.app-badge--info { background: var(--status-running-bg); border-color: var(--status-running-border); color: var(--status-running-text); }
.app-badge--success { background: var(--status-success-bg); border-color: var(--status-success-border); color: var(--status-success-text); }
.app-badge--warning { background: var(--status-waiting-bg); border-color: var(--status-waiting-border); color: var(--status-waiting-text); }
.app-badge--danger { background: var(--status-danger-bg); border-color: var(--status-danger-border); color: var(--status-danger-text); }
</style>
