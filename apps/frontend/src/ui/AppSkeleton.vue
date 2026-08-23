<script setup lang="ts">
withDefaults(defineProps<{
  readonly label?: string;
  readonly lines?: number;
}>(), { label: "正在加载", lines: 3 });
</script>

<template>
  <div class="app-skeleton" role="status" :aria-label="label">
    <span v-for="line in lines" :key="line" :style="{ width: `${100 - ((line - 1) % 3) * 13}%` }" />
  </div>
</template>

<style scoped>
.app-skeleton { display: grid; gap: 0.65rem; overflow: hidden; }
.app-skeleton span { animation: skeleton-pulse 1.3s ease-in-out infinite alternate; background: var(--surface-inset); border-radius: 0.35rem; display: block; height: 0.85rem; }
@keyframes skeleton-pulse { to { opacity: 0.48; } }
</style>
