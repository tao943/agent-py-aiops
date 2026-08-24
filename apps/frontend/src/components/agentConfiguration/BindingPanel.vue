<script setup lang="ts">
import { History, Link2 } from "lucide-vue-next";
import type { AgentBinding, AgentNode, AgentResource, AgentResourceVersion } from "@agent-py/api-contracts";
import AppBadge from "../../ui/AppBadge.vue";

defineProps<{ readonly node: AgentNode; readonly binding: AgentBinding | null; readonly resources: readonly AgentResource[]; readonly versions: readonly AgentResourceVersion[]; readonly isBinding: boolean; readonly canManage: boolean; }>();
const emit = defineEmits<{ bind: [versionId: string] }>();
function nameFor(version: AgentResourceVersion, resources: readonly AgentResource[]): string { return resources.find((item) => item.id === version.resourceId)?.name ?? "未知资源"; }
function isBound(version: AgentResourceVersion, binding: AgentBinding | null): boolean { return binding?.promptVersionId === version.id || binding?.skillVersionIds.includes(version.id) === true; }
</script>

<template>
  <section class="binding-panel" aria-label="节点绑定">
    <header><div><Link2 :size="16" /><strong>运行绑定</strong></div><AppBadge tone="info">{{ node }}</AppBadge></header>
    <p>运行时只加载已发布且与节点兼容的版本。历史回滚通过重新绑定完成，不会修改旧版本。</p>
    <div class="binding-panel__versions">
      <article v-for="version in versions" :key="version.id">
        <span><strong>{{ nameFor(version, resources) }}</strong><small>v{{ version.version }} · {{ version.status }}</small></span>
        <AppBadge v-if="isBound(version, binding)" tone="success">当前绑定</AppBadge>
        <button v-else-if="canManage" type="button" :disabled="isBinding" :aria-label="`重新绑定版本 ${version.version}`" @click="emit('bind', version.id)"><History :size="14" />绑定</button>
      </article>
      <div v-if="versions.length === 0" class="binding-panel__empty">暂无可绑定的已发布版本。</div>
    </div>
  </section>
</template>

<style scoped>
.binding-panel { border-bottom: 1px solid var(--line); padding: 1rem; }
.binding-panel header, .binding-panel header > div, .binding-panel article, .binding-panel button { align-items: center; display: flex; }
.binding-panel header { justify-content: space-between; }
.binding-panel header > div { gap: 0.45rem; }
.binding-panel header strong { font-size: 0.78rem; }
.binding-panel > p { color: var(--text-secondary); font-size: 0.68rem; line-height: 1.55; margin: 0.65rem 0 0.8rem; }
.binding-panel__versions { display: grid; gap: 0.45rem; }
.binding-panel article { background: var(--surface-panel); border: 1px solid var(--line); border-radius: var(--radius-control); justify-content: space-between; min-height: 3.25rem; padding: 0.55rem 0.65rem; }
.binding-panel article > span { display: grid; gap: 0.2rem; min-width: 0; }
.binding-panel article strong { font-size: 0.72rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.binding-panel article small, .binding-panel__empty { color: var(--text-tertiary); font-size: 0.65rem; }
.binding-panel button { border-radius: var(--radius-control); color: var(--accent); font-size: 0.68rem; font-weight: 700; gap: 0.25rem; min-height: 2.5rem; padding: 0 0.55rem; }
.binding-panel button:hover { background: var(--surface-selected); }
</style>
