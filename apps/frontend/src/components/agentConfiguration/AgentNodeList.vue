<script setup lang="ts">
import { LockKeyhole, MessageSquareText } from "lucide-vue-next";

import type { AgentNode } from "@agent-py/api-contracts";

defineProps<{ readonly selected: AgentNode }>();
const emit = defineEmits<{ select: [node: AgentNode] }>();

const nodes: readonly { readonly id: AgentNode; readonly label: string; readonly managed: "user" | "system" }[] = [
  { id: "conversation", label: "对话主编排", managed: "user" },
  { id: "planner", label: "诊断规划", managed: "system" },
  { id: "replanner", label: "动态重规划", managed: "system" },
  { id: "investigator_runtime", label: "运行时调查", managed: "system" },
  { id: "investigator_log", label: "日志调查", managed: "system" },
  { id: "investigator_change", label: "变更调查", managed: "system" },
  { id: "adjudicator", label: "证据裁决", managed: "system" },
  { id: "validator", label: "语义核验", managed: "system" },
  { id: "recovery_planner", label: "恢复规划", managed: "system" },
  { id: "report", label: "报告生成", managed: "system" }
];
</script>

<template>
  <nav class="agent-nodes" aria-label="Agent 节点">
    <div class="agent-nodes__heading"><span>执行节点</span><small>1 个可配置</small></div>
    <button
      v-for="node in nodes"
      :key="node.id"
      type="button"
      :class="{ 'agent-nodes__item--active': selected === node.id }"
      :aria-current="selected === node.id ? 'page' : undefined"
      @click="emit('select', node.id)"
    >
      <MessageSquareText v-if="node.managed === 'user'" :size="16" aria-hidden="true" />
      <LockKeyhole v-else :size="15" aria-hidden="true" />
      <span>{{ node.label }}</span>
      <small>{{ node.managed === "user" ? "用户配置" : "服务端受控" }}</small>
    </button>
  </nav>
</template>

<style scoped>
.agent-nodes { border-right: 1px solid var(--line); min-width: 0; overflow-y: auto; padding: 1rem 0.75rem; }
.agent-nodes__heading { align-items: baseline; display: flex; justify-content: space-between; padding: 0 0.55rem 0.65rem; }
.agent-nodes__heading span { font-size: 0.72rem; font-weight: 760; letter-spacing: 0.04em; }
.agent-nodes__heading small { color: var(--text-tertiary); font-size: 0.66rem; }
.agent-nodes__item--active, .agent-nodes button:hover { background: var(--surface-selected); }
.agent-nodes button { align-items: center; border-radius: var(--radius-control); display: grid; gap: 0.5rem; grid-template-columns: auto minmax(0, 1fr); min-height: 2.9rem; padding: 0.5rem 0.55rem; text-align: left; width: 100%; }
.agent-nodes button span { font-size: 0.78rem; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-nodes button small { color: var(--text-tertiary); font-size: 0.63rem; grid-column: 2; }
@media (max-width: 900px) { .agent-nodes { border-bottom: 1px solid var(--line); border-right: 0; display: flex; gap: 0.35rem; overflow-x: auto; padding: 0.7rem 1rem; } .agent-nodes__heading { display: none; } .agent-nodes button { flex: 0 0 auto; grid-template-columns: auto auto; width: auto; } .agent-nodes button small { display: none; } }
</style>
