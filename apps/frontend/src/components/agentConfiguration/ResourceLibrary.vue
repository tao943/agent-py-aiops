<script setup lang="ts">
import { FileCode2, Plus, Workflow } from "lucide-vue-next";

import type { AgentResource, AgentResourceKind, AgentResourceVersion } from "@agent-py/api-contracts";

defineProps<{
  readonly resources: readonly AgentResource[];
  readonly versions: readonly AgentResourceVersion[];
  readonly selectedResourceId: string | null;
  readonly canCreate: boolean;
}>();
const emit = defineEmits<{ select: [id: string]; create: [kind: AgentResourceKind] }>();

function latestStatus(resourceId: string, versions: readonly AgentResourceVersion[]): string {
  return versions.filter((item) => item.resourceId === resourceId).sort((a, b) => b.version - a.version)[0]?.status ?? "empty";
}
</script>

<template>
  <section class="resource-library" aria-label="配置资源库">
    <header>
      <div><span>资源库</span><small>{{ resources.length }} 项</small></div>
      <div v-if="canCreate" class="resource-library__create">
        <button type="button" aria-label="新建 Prompt" title="新建 Prompt" @click="emit('create', 'prompt')"><Plus :size="15" /><FileCode2 :size="15" /></button>
        <button type="button" aria-label="新建 Skill" title="新建 Skill" @click="emit('create', 'skill')"><Plus :size="15" /><Workflow :size="15" /></button>
      </div>
    </header>
    <div v-if="resources.length === 0" class="resource-library__empty">该节点暂无配置资源。</div>
    <button
      v-for="resource in resources"
      :key="resource.id"
      type="button"
      class="resource-library__item"
      :class="{ 'resource-library__item--active': resource.id === selectedResourceId }"
      @click="emit('select', resource.id)"
    >
      <FileCode2 v-if="resource.kind === 'prompt'" :size="16" aria-hidden="true" />
      <Workflow v-else :size="16" aria-hidden="true" />
      <span><strong>{{ resource.name }}</strong><small>{{ resource.kind === "prompt" ? "Prompt" : "Skill" }} · {{ latestStatus(resource.id, versions) }}</small></span>
    </button>
  </section>
</template>

<style scoped>
.resource-library { border-right: 1px solid var(--line); min-width: 0; overflow-y: auto; padding: 1rem 0.8rem; }
.resource-library header, .resource-library header > div { align-items: center; display: flex; justify-content: space-between; }
.resource-library header { margin-bottom: 0.7rem; padding-inline: 0.4rem; }
.resource-library header > div:first-child { gap: 0.45rem; }
.resource-library header span { font-size: 0.72rem; font-weight: 760; }
.resource-library header small { color: var(--text-tertiary); font-size: 0.67rem; }
.resource-library__create { gap: 0.2rem; }
.resource-library__create button { align-items: center; border-radius: var(--radius-control); display: inline-flex; min-height: 2.5rem; padding: 0 0.45rem; }
.resource-library__create button:hover { background: var(--surface-hover); }
.resource-library__item { align-items: center; border-radius: var(--radius-control); display: grid; gap: 0.6rem; grid-template-columns: auto minmax(0, 1fr); min-height: 3.7rem; padding: 0.65rem; text-align: left; width: 100%; }
.resource-library__item:hover, .resource-library__item--active { background: var(--surface-selected); }
.resource-library__item span { display: grid; gap: 0.22rem; min-width: 0; }
.resource-library__item strong { font-size: 0.78rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.resource-library__item small, .resource-library__empty { color: var(--text-tertiary); font-size: 0.67rem; }
.resource-library__empty { padding: 1rem 0.4rem; }
@media (max-width: 1100px) { .resource-library { border-bottom: 1px solid var(--line); border-right: 0; display: flex; gap: 0.45rem; overflow-x: auto; padding: 0.7rem 1rem; } .resource-library header { flex: 0 0 auto; margin: 0; } .resource-library header > div:first-child { display: none; } .resource-library__item { flex: 0 0 13rem; } }
</style>
