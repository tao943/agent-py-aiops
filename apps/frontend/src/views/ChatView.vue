<script setup lang="ts">
import { Activity, Settings2 } from "lucide-vue-next";
import { computed, onMounted, ref } from "vue";
import { RouterLink, useRouter } from "vue-router";

import type { ReferenceSourceSseEvent } from "@agent-py/api-contracts";
import ChatComposer from "../components/ChatComposer.vue";
import ChatTranscript from "../components/ChatTranscript.vue";
import { useChatStore } from "../stores/chat";
import AppDrawer from "../ui/AppDrawer.vue";

const chat = useChatStore();
const router = useRouter();
const detailsOpen = ref(false);
const detailsButton = ref<HTMLButtonElement | null>(null);
const activeTitle = computed(() => chat.sessions.find((session) => session.id === chat.activeSessionId)?.title ?? "新对话");

onMounted(() => { void chat.initialize().catch(() => undefined); });
function run(operation: () => Promise<unknown>): void { void operation().catch(() => undefined); }
function openCitationDocument(reference: ReferenceSourceSseEvent["reference"]): void {
  if (reference.documentId === undefined || reference.knowledgeBaseId === undefined) return;
  void router.push({ name: "knowledge", query: { documentId: reference.documentId, knowledgeBaseId: reference.knowledgeBaseId } });
}
</script>

<template>
  <section class="chat-view" aria-label="对话工作区">
    <div class="chat-view__conversation">
      <header>
        <div><p>运维助手</p><h2>{{ activeTitle }}</h2></div>
        <button ref="detailsButton" type="button" aria-label="查看运行详情" @click="detailsOpen = true"><Activity :size="16" />运行详情</button>
      </header>
      <ChatTranscript :diagnostic-results="chat.diagnosticResults" :is-loading="chat.isLoading" :live-tool-calls="chat.liveToolCalls" :messages="chat.messages" :pending-action-loading-ids="chat.pendingActionLoadingIds" :pending-actions="chat.pendingActions" :references="chat.references" :tool-audits="chat.toolAudits" @cancel-action="run(() => chat.cancelPendingAction($event))" @confirm-action="run(() => chat.confirmPendingAction($event))" @open-document="openCitationDocument" />
      <ChatComposer :disabled="chat.isLoading" :is-sending="chat.isSending" :is-updating-memory="chat.isUpdatingMemory" :memory="chat.activeSession?.memory ?? null" @apply-memory="run(() => chat.updateMemoryMode($event))" @compact-memory="run(() => chat.compactMemory())" @send="run(() => chat.send($event))" />
    </div>
    <AppDrawer :open="detailsOpen" title="运行详情" :return-focus-to="detailsButton" @close="detailsOpen = false">
      <div class="run-details">
        <section><span>当前会话</span><strong>{{ chat.activeSessionId ?? "尚未创建" }}</strong></section>
        <section><span>记忆策略</span><strong>{{ chat.activeSession?.memory.mode ?? "尚不可用" }}</strong><small v-if="chat.activeSession">上下文 {{ chat.activeSession.memory.contextUsagePercent }}%</small></section>
        <section><span>工具活动</span><strong>{{ chat.liveToolCalls.length ? `${chat.liveToolCalls.length} 项执行中` : `${chat.toolAudits.length} 项已记录` }}</strong></section>
        <section><span>知识引用</span><strong>{{ chat.references.length }} 条</strong></section>
        <section><span>Prompt 版本</span><strong>{{ chat.lastRun?.agentConfigurationSnapshot?.promptVersionId ?? "本次 Run 暂无版本快照" }}</strong></section>
        <section><span>Skill 版本</span><strong>{{ chat.lastRun?.agentConfigurationSnapshot?.skillVersionIds.join(", ") || "未绑定" }}</strong></section>
        <p>每次新 Run 都在服务端保存已发布 Prompt/Skill 的不可变版本快照；核心诊断节点仍由服务端 Policy Gate 约束。</p>
        <RouterLink to="/agent-config?node=conversation"><Settings2 :size="16" />管理对话 Agent 配置</RouterLink>
      </div>
    </AppDrawer>
  </section>
</template>

<style scoped>
.chat-view { background: var(--surface-raised); display: grid; grid-template-columns: minmax(0, 1fr); height: 100%; overflow: hidden; }
.chat-view__conversation { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; min-height: 0; min-width: 0; }
.chat-view__conversation > header { align-items: center; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; min-height: 3.75rem; padding: 0.65rem clamp(1rem, 3vw, 2rem); }
.chat-view__conversation > header div { min-width: 0; }
.chat-view__conversation > header p { color: var(--accent); font-size: 0.65rem; font-weight: 780; letter-spacing: 0.07em; margin: 0; }
.chat-view__conversation > header h2 { font-size: 0.92rem; font-weight: 660; margin: 0.15rem 0 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chat-view__conversation > header button { align-items: center; border: 1px solid var(--line-strong); border-radius: var(--radius-control); display: inline-flex; font-size: 0.72rem; font-weight: 650; gap: 0.4rem; min-height: 2.6rem; padding: 0 0.75rem; }
.chat-view__conversation > header button:hover { background: var(--surface-hover); }
.run-details { display: grid; gap: 0; }
.run-details section { border-bottom: 1px solid var(--line); display: grid; gap: 0.25rem; padding: 0.8rem 0; }
.run-details span, .run-details small { color: var(--text-tertiary); font-size: 0.67rem; }
.run-details strong { font-size: 0.78rem; overflow-wrap: anywhere; }
.run-details p { color: var(--text-secondary); font-size: 0.72rem; line-height: 1.6; margin: 1rem 0; }
.run-details a { align-items: center; background: var(--accent); border-radius: var(--radius-control); color: white; display: inline-flex; font-size: 0.74rem; font-weight: 700; gap: 0.45rem; justify-content: center; min-height: 2.75rem; text-decoration: none; }
@media (max-width: 700px) { .chat-view { height: auto; min-height: 100%; } .chat-view__conversation { min-height: 42rem; } }
</style>
