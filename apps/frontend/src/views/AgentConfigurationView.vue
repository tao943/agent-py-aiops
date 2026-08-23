<script setup lang="ts">
import { onMounted } from "vue";

import ChatPromptSidebar from "../components/ChatPromptSidebar.vue";
import ChatSkillSidebar from "../components/ChatSkillSidebar.vue";
import { useChatStore } from "../stores/chat";

const chat = useChatStore();

onMounted(() => {
  void chat.initialize().catch(() => undefined);
});

function run(operation: () => Promise<unknown>): void {
  void operation().catch(() => undefined);
}

function saveConfiguration(systemPromptId: string, skillIds: readonly string[]): void {
  run(() => chat.saveConfiguration(systemPromptId, skillIds));
}
</script>

<template>
  <section class="agent-config-compat" aria-label="Agent 配置">
    <header>
      <div><span>Agent 配置</span><h2>Prompt 与 Skill</h2></div>
      <p>当前兼容已有配置；版本、发布、节点绑定与审计将在配置闭环切片中替换。</p>
    </header>
    <div class="agent-config-compat__grid">
      <ChatPromptSidebar
        :configuration="chat.configuration"
        :is-saving="chat.isSavingConfiguration"
        @create-prompt="(label, content) => run(() => chat.createPrompt(label, content))"
        @delete-prompt="run(() => chat.deletePrompt($event))"
        @save="saveConfiguration"
        @update-prompt="(promptId, label, content) => run(() => chat.updatePrompt(promptId, label, content))"
      />
      <ChatSkillSidebar
        :configuration="chat.configuration"
        :is-saving="chat.isSavingConfiguration"
        @delete-skill="run(() => chat.deleteSkill($event))"
        @save="saveConfiguration"
        @upload-skill="run(() => chat.uploadSkill($event))"
      />
    </div>
  </section>
</template>

<style scoped>
.agent-config-compat { background: var(--surface-canvas); display: grid; grid-template-rows: auto minmax(0, 1fr); height: 100%; min-height: 0; }
.agent-config-compat > header { align-items: center; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; padding: 1rem 1.5rem; }
.agent-config-compat span { color: var(--accent); font-size: 0.7rem; font-weight: 760; }
.agent-config-compat h2 { font-size: 1rem; margin: 0.15rem 0 0; }
.agent-config-compat p { color: var(--text-secondary); font-size: 0.76rem; margin: 0; max-width: 40rem; }
.agent-config-compat__grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); min-height: 0; overflow: hidden; }
@media (max-width: 900px) { .agent-config-compat__grid { grid-template-columns: 1fr; overflow: auto; } }
</style>
