<script setup lang="ts">
import { Database, Layers3 } from "lucide-vue-next";
import type { IncidentDetail } from "@agent-py/api-contracts";
import type { PublicInvestigationResult } from "../../stores/aiops";
defineProps<{ readonly incident: IncidentDetail; readonly result: PublicInvestigationResult }>();
</script>

<template>
  <aside class="context" aria-label="调查上下文">
    <section><header><Layers3 :size="16" aria-hidden="true" /><h3>运行上下文</h3></header><dl><div><dt>Agent 模式</dt><dd>{{ result.agentMode === "unknown" ? "未提供" : `${result.agentMode === "multi" ? "Multi" : "Single"}-Agent` }}</dd></div><div><dt>当前阶段</dt><dd>{{ incident.currentStage }}</dd></div><div><dt>诊断状态</dt><dd>{{ incident.diagnosticStatus ?? "未启动" }}</dd></div><div><dt>影响范围</dt><dd>{{ incident.service ?? "未知服务" }} · {{ incident.environment ?? "环境未知" }}</dd></div></dl></section>
    <section><header><Database :size="16" aria-hidden="true" /><h3>持久化元数据</h3></header><dl><div><dt>Checkpoint 数量</dt><dd>{{ incident.evidenceChain?.checkpoints.length ?? 0 }}</dd></div><div><dt>证据数量</dt><dd>{{ incident.evidenceChain?.evidence.length ?? 0 }}</dd></div><div><dt>工具调用</dt><dd>{{ incident.evidenceChain?.toolCalls.length ?? 0 }}</dd></div><div><dt>配置版本</dt><dd>{{ result.configurationVersionIds.length ? result.configurationVersionIds.join("、") : "未提供" }}</dd></div></dl><p>仅展示计数和版本标识，不展示 checkpoint state。</p></section>
  </aside>
</template>

<style scoped>
.context { background: var(--surface-panel); border-left: 1px solid var(--line); min-width: 0; padding: 1rem; }
section + section { border-top: 1px solid var(--line); margin-top: 1rem; padding-top: 1rem; }
header { align-items: center; display: flex; gap: 0.45rem; } header svg { color: var(--accent); } h3 { font-size: 0.8rem; margin: 0; }
dl { margin: 0.65rem 0 0; } dl div { align-items: baseline; display: grid; gap: 0.6rem; grid-template-columns: 6rem minmax(0, 1fr); padding: 0.38rem 0; }
dt { color: var(--text-tertiary); font-size: 0.68rem; } dd { font-size: 0.72rem; margin: 0; overflow-wrap: anywhere; }
p { color: var(--text-tertiary); font-size: 0.68rem; line-height: 1.5; margin: 0.65rem 0 0; }
@media (max-width: 980px) { .context { border-left: 0; border-top: 1px solid var(--line); } }
</style>
