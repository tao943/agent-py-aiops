<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";

import AiopsView from "./AiopsView.vue";

const route = useRoute();
const incidentId = computed(() => String(route.params.incidentId ?? ""));
</script>

<template>
  <section class="incident-compat" aria-label="调查工作台">
    <header>
      <div><span>调查工作台</span><h2>{{ incidentId }}</h2></div>
      <p>证据链与正式恢复状态将在调查工作台切片中接入。</p>
    </header>
    <div class="incident-compat__body"><AiopsView /></div>
  </section>
</template>

<style scoped>
.incident-compat { display: grid; grid-template-rows: auto minmax(0, 1fr); height: 100%; min-height: 0; }
.incident-compat > header { align-items: center; background: var(--surface-panel); border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; padding: 0.8rem 1.25rem; }
.incident-compat span { color: var(--accent); font-size: 0.7rem; font-weight: 760; }
.incident-compat h2 { font-size: 0.95rem; margin: 0.15rem 0 0; }
.incident-compat p { color: var(--text-secondary); font-size: 0.75rem; margin: 0; }
.incident-compat__body { min-height: 0; overflow: hidden; }
</style>
