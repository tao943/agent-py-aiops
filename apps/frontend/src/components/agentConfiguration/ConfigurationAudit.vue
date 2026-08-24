<script setup lang="ts">
import { Clock3 } from "lucide-vue-next";
import type { AgentConfigurationAuditEvent } from "@agent-py/api-contracts";
defineProps<{ readonly events: readonly AgentConfigurationAuditEvent[] }>();
const labels: Record<AgentConfigurationAuditEvent["action"], string> = { resource_created: "创建资源", draft_saved: "保存草稿", version_published: "发布版本", version_deprecated: "停用版本", binding_updated: "更新绑定" };
</script>

<template>
  <section class="configuration-audit" aria-label="配置审计">
    <header><Clock3 :size="16" /><strong>最近变更</strong></header>
    <ol v-if="events.length"><li v-for="event in events.slice(0, 8)" :key="event.id"><span>{{ labels[event.action] }}</span><time :datetime="event.createdAt">{{ new Date(event.createdAt).toLocaleString("zh-CN", { hour12: false }) }}</time></li></ol>
    <p v-else>暂无配置变更记录。</p>
  </section>
</template>

<style scoped>
.configuration-audit { min-height: 0; overflow-y: auto; padding: 1rem; }
.configuration-audit header { align-items: center; display: flex; gap: 0.45rem; }
.configuration-audit header strong { font-size: 0.78rem; }
.configuration-audit ol { list-style: none; margin: 0.8rem 0 0; padding: 0; }
.configuration-audit li { border-top: 1px solid var(--line); display: grid; gap: 0.2rem; padding: 0.65rem 0; }
.configuration-audit li span { font-size: 0.7rem; font-weight: 650; }
.configuration-audit time, .configuration-audit p { color: var(--text-tertiary); font-size: 0.65rem; }
</style>
