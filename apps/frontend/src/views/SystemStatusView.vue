<script setup lang="ts">
import { CircleCheck, RefreshCw, TriangleAlert } from "lucide-vue-next";
import { onMounted, ref } from "vue";

import AppButton from "../ui/AppButton.vue";
import AppSkeleton from "../ui/AppSkeleton.vue";
import { createRuntimeHealthClient, type RuntimeHealth } from "../runtimeHealth";

const health = ref<RuntimeHealth | null>(null);
const error = ref<string | null>(null);
const loading = ref(false);

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    health.value = await createRuntimeHealthClient().health();
  } catch {
    error.value = "无法读取服务状态，请确认本地后端已启动。";
  } finally {
    loading.value = false;
  }
}

onMounted(() => { void load(); });
</script>

<template>
  <section class="system-status" aria-label="系统状态">
    <header><div><span>系统状态</span><h2>运行时连接</h2></div><AppButton size="small" @click="load"><RefreshCw :size="15" aria-hidden="true" />刷新</AppButton></header>
    <AppSkeleton v-if="loading && health === null" label="正在读取系统状态" />
    <div v-else-if="error" class="system-status__state system-status__state--error" role="alert"><TriangleAlert :size="20" aria-hidden="true" /><div><strong>服务连接异常</strong><p>{{ error }}</p></div></div>
    <div v-else-if="health" class="system-status__state" role="status"><CircleCheck :size="20" aria-hidden="true" /><div><strong>后端服务正常</strong><p>{{ health.service }} · {{ health.version }}</p></div></div>
  </section>
</template>

<style scoped>
.system-status { background: var(--surface-canvas); height: 100%; overflow: auto; padding: clamp(1rem, 3vw, 2rem); }
.system-status > header { align-items: center; display: flex; justify-content: space-between; margin-bottom: 1.25rem; }
.system-status span { color: var(--accent); font-size: 0.7rem; font-weight: 760; }
.system-status h2 { font-size: 1.2rem; margin: 0.2rem 0 0; }
.system-status__state { align-items: flex-start; background: var(--surface-raised); border: 1px solid var(--line); border-radius: var(--radius-panel); color: var(--status-success-text); display: flex; gap: 0.8rem; max-width: 42rem; padding: 1.2rem; }
.system-status__state--error { color: var(--status-danger-text); }
.system-status__state strong { color: var(--text-primary); }
.system-status__state p { color: var(--text-secondary); font-size: 0.8rem; margin: 0.3rem 0 0; }
</style>
