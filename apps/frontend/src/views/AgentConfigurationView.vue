<script setup lang="ts">
import { AlertTriangle, Settings2 } from "lucide-vue-next";
import { computed, onMounted } from "vue";
import { onBeforeRouteLeave, useRoute, useRouter } from "vue-router";

import type { AgentNode } from "@agent-py/api-contracts";
import AgentNodeList from "../components/agentConfiguration/AgentNodeList.vue";
import BindingPanel from "../components/agentConfiguration/BindingPanel.vue";
import ConfigurationAudit from "../components/agentConfiguration/ConfigurationAudit.vue";
import ResourceLibrary from "../components/agentConfiguration/ResourceLibrary.vue";
import VersionEditor from "../components/agentConfiguration/VersionEditor.vue";
import AppEmptyState from "../components/AppEmptyState.vue";
import AppErrorState from "../components/AppErrorState.vue";
import AppLoadingState from "../components/AppLoadingState.vue";
import { useAgentConfigurationStore } from "../stores/agentConfiguration";

const route = useRoute();
const router = useRouter();
const store = useAgentConfigurationStore();
const allowedNodes = new Set<AgentNode>(["conversation", "planner", "replanner", "investigator_runtime", "investigator_log", "investigator_change", "adjudicator", "validator", "recovery_planner", "report"]);
const routeNode = computed(() => typeof route.query.node === "string" && allowedNodes.has(route.query.node as AgentNode) ? route.query.node as AgentNode : "conversation");
const userConfigurable = computed(() => store.selectedNode === "conversation");
const visibleResources = computed(() => store.resources.filter((resource) => store.versions.some((version) => version.resourceId === resource.id && Array.isArray(version.spec.bindableNodes) && version.spec.bindableNodes.includes(store.selectedNode))));

onMounted(() => { void store.initialize({ node: routeNode.value }).catch(() => undefined); });
onBeforeRouteLeave(() => !store.dirty || window.confirm("当前草稿尚未保存，确定离开吗？"));

function run(operation: () => Promise<unknown>): void { void operation().catch(() => undefined); }
function selectNode(node: AgentNode): void {
  if (store.dirty && !window.confirm("当前草稿尚未保存，确定切换节点吗？")) return;
  store.selectedNode = node;
  void router.replace({ query: { ...route.query, node } });
  const first = visibleResources.value[0];
  if (first !== undefined) store.selectResource(first.id);
}
</script>

<template>
  <section class="agent-config" aria-label="Agent 配置">
    <header class="agent-config__header">
      <div><span>AGENT CONTROL PLANE</span><h1>配置控制台</h1><p>发布、绑定并审计对话主编排。AIOps 核心节点保持服务端版本化控制。</p></div>
      <div class="agent-config__scope"><Settings2 :size="17" /><span>Owner 隔离</span></div>
    </header>
    <AppLoadingState v-if="store.isLoading" label="正在加载 Agent 配置" />
    <AppErrorState v-else-if="store.errorMessage && store.resources.length === 0" :message="store.errorMessage" :can-retry="true" @retry="run(() => store.initialize({ node: routeNode }))" />
    <div v-else class="agent-config__workspace">
      <AgentNodeList :selected="store.selectedNode" @select="selectNode" />
      <ResourceLibrary :resources="visibleResources" :versions="store.versions" :selected-resource-id="store.selectedResourceId" :can-create="store.canManageConfiguration && userConfigurable" @select="store.selectResource" @create="run(() => store.createResource($event))" />
      <div v-if="!userConfigurable" class="agent-config__managed">
        <AlertTriangle :size="20" /><h2>服务端受控节点</h2><p>该节点使用仓库内经过测试和版本管理的专用 Prompt，不接受用户配置，避免绕过证据链、Validator 与 Policy Gate。</p>
      </div>
      <VersionEditor v-else-if="store.selectedResource && store.selectedVersion" :resource="store.selectedResource" :version="store.selectedVersion" :versions="store.resourceVersions" :draft="store.draft" :dirty="store.dirty" :validation="store.validation" :can-manage="store.canManageConfiguration" :is-saving="store.isSaving" :is-publishing="store.isPublishing" @update-draft="store.updateDraft" @select-version="store.selectVersion" @edit="run(store.beginEditingSelected)" @save="run(store.saveDraft)" @validate="run(store.validateSelected)" @publish="run(store.publishSelected)" @deprecate="run(store.deprecateSelected)" />
      <AppEmptyState v-else title="暂无 conversation 配置" detail="创建 Prompt 或 Skill 草稿，校验并发布后再绑定到对话入口。" />
      <aside class="agent-config__context">
        <BindingPanel :node="store.selectedNode" :binding="store.selectedBinding" :resources="store.resources" :versions="store.compatiblePublishedVersions" :is-binding="store.isBinding" :can-manage="store.canManageConfiguration && userConfigurable" @bind="run(() => store.bindVersion($event))" />
        <ConfigurationAudit :events="store.auditEvents" />
      </aside>
    </div>
  </section>
</template>

<style scoped>
.agent-config { background: var(--surface-raised); display: grid; grid-template-rows: auto minmax(0, 1fr); height: 100%; min-height: 0; }
.agent-config__header { align-items: center; background: var(--surface-panel); border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; padding: 1rem 1.4rem; }
.agent-config__header > div:first-child > span { color: var(--accent); font-size: 0.62rem; font-weight: 800; letter-spacing: 0.12em; }
.agent-config__header h1 { font-size: 1.08rem; margin: 0.15rem 0; }
.agent-config__header p { color: var(--text-secondary); font-size: 0.72rem; margin: 0; }
.agent-config__scope { align-items: center; color: var(--text-secondary); display: flex; font-size: 0.7rem; gap: 0.4rem; }
.agent-config__workspace { display: grid; grid-template-columns: minmax(10rem, 13rem) minmax(13rem, 16rem) minmax(25rem, 1fr) minmax(16rem, 20rem); min-height: 0; }
.agent-config__context { background: var(--surface-raised); border-left: 1px solid var(--line); display: grid; grid-template-rows: auto minmax(0, 1fr); min-height: 0; }
.agent-config__managed { align-content: center; display: grid; justify-items: start; max-width: 36rem; padding: 2rem; }
.agent-config__managed svg { color: var(--warning); }
.agent-config__managed h2 { font-size: 1rem; margin: 0.8rem 0 0.4rem; }
.agent-config__managed p { color: var(--text-secondary); font-size: 0.75rem; line-height: 1.65; margin: 0; }
@media (max-width: 1100px) { .agent-config { height: auto; min-height: 100%; } .agent-config__workspace { grid-template-columns: 1fr minmax(17rem, 22rem); grid-template-rows: auto auto minmax(34rem, auto); } .agent-config__workspace > :first-child, .agent-config__workspace > :nth-child(2) { grid-column: 1 / -1; } .agent-config__context { grid-column: 2; grid-row: 3; } }
@media (max-width: 700px) { .agent-config__header { align-items: flex-start; flex-direction: column; gap: 0.7rem; } .agent-config__workspace { display: block; } .agent-config__context { border-left: 0; border-top: 1px solid var(--line); } }
</style>
