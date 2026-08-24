<script setup lang="ts">
import { CheckCircle2, CopyPlus, Save, Send } from "lucide-vue-next";
import { computed } from "vue";

import type { AgentResource, AgentResourceVersion } from "@agent-py/api-contracts";
import type { AgentDraftEditor } from "../../stores/agentConfiguration";
import type { AgentVersionValidationResponse } from "../../agentConfiguration/agentConfigurationClient";
import AppBadge from "../../ui/AppBadge.vue";
import AppButton from "../../ui/AppButton.vue";

const props = defineProps<{
  readonly resource: AgentResource;
  readonly version: AgentResourceVersion;
  readonly versions: readonly AgentResourceVersion[];
  readonly draft: AgentDraftEditor;
  readonly dirty: boolean;
  readonly validation: AgentVersionValidationResponse | null;
  readonly canManage: boolean;
  readonly isSaving: boolean;
  readonly isPublishing: boolean;
}>();
const emit = defineEmits<{
  updateDraft: [patch: Partial<AgentDraftEditor>];
  selectVersion: [id: string];
  edit: [];
  save: [];
  validate: [];
  publish: [];
  deprecate: [];
}>();

const isDraft = computed(() => props.version.status === "draft");
const specText = computed(() => JSON.stringify(props.draft.spec, null, 2));

function updateSpec(event: Event): void {
  const raw = (event.target as HTMLTextAreaElement).value;
  try { emit("updateDraft", { spec: JSON.parse(raw) as Record<string, unknown> }); } catch { /* keep the last valid spec */ }
}
</script>

<template>
  <article class="version-editor" aria-label="版本编辑器">
    <header>
      <div>
        <span class="version-editor__eyebrow">{{ resource.kind === "prompt" ? "PROMPT" : "SKILL" }}</span>
        <h2>{{ resource.name }}</h2>
        <p>{{ resource.description || "暂无说明" }}</p>
      </div>
      <div class="version-editor__version">
        <label for="agent-version">版本</label>
        <select id="agent-version" :value="version.id" @change="emit('selectVersion', ($event.target as HTMLSelectElement).value)">
          <option v-for="item in versions" :key="item.id" :value="item.id">v{{ item.version }} · {{ item.status }}</option>
        </select>
        <AppBadge :tone="version.status === 'published' ? 'success' : version.status === 'deprecated' ? 'neutral' : 'warning'">{{ version.status }}</AppBadge>
      </div>
    </header>

    <div class="version-editor__toolbar">
      <span>{{ isDraft ? (dirty ? "有未保存更改" : "草稿已保存") : "已发布版本不可修改" }}</span>
      <div>
        <AppButton v-if="!isDraft && canManage" size="small" @click="emit('edit')"><CopyPlus :size="15" />创建新草稿</AppButton>
        <AppButton v-if="isDraft && canManage" size="small" :loading="isSaving" :disabled="!dirty" @click="emit('save')"><Save :size="15" />保存</AppButton>
        <AppButton v-if="isDraft && canManage" size="small" :disabled="dirty" @click="emit('validate')"><CheckCircle2 :size="15" />校验</AppButton>
        <AppButton v-if="isDraft && canManage" variant="primary" size="small" :loading="isPublishing" :disabled="dirty || validation?.valid !== true" @click="emit('publish')"><Send :size="15" />发布</AppButton>
      </div>
    </div>

    <div class="version-editor__fields">
      <label>名称<input :readonly="!isDraft" :value="draft.name" @input="emit('updateDraft', { name: ($event.target as HTMLInputElement).value })" /></label>
      <label>说明<input :readonly="!isDraft" :value="draft.description" @input="emit('updateDraft', { description: ($event.target as HTMLInputElement).value })" /></label>
      <label class="version-editor__content">内容<textarea :readonly="!isDraft" :value="draft.content" spellcheck="false" @input="emit('updateDraft', { content: ($event.target as HTMLTextAreaElement).value })" /></label>
      <label class="version-editor__spec">执行约束（JSON）<textarea :readonly="!isDraft" :value="specText" spellcheck="false" @input="updateSpec" /></label>
    </div>
    <p v-if="validation?.valid" class="version-editor__valid" role="status"><CheckCircle2 :size="15" />校验通过{{ validation.warnings.length ? `，${validation.warnings.length} 条提醒` : "" }}</p>
  </article>
</template>

<style scoped>
.version-editor { display: grid; grid-template-rows: auto auto minmax(0, 1fr) auto; min-height: 0; overflow: hidden; }
.version-editor > header { align-items: flex-start; border-bottom: 1px solid var(--line); display: flex; gap: 1rem; justify-content: space-between; padding: 1.15rem 1.4rem; }
.version-editor__eyebrow { color: var(--accent); font-size: 0.65rem; font-weight: 800; letter-spacing: 0.09em; }
.version-editor h2 { font-size: 1.05rem; margin: 0.2rem 0; }
.version-editor p { color: var(--text-secondary); font-size: 0.73rem; margin: 0; }
.version-editor__version { align-items: center; display: flex; gap: 0.5rem; }
.version-editor__version > label { clip: rect(0 0 0 0); clip-path: inset(50%); height: 1px; overflow: hidden; position: absolute; white-space: nowrap; width: 1px; }
.version-editor select, .version-editor input, .version-editor textarea { background: var(--surface-raised); border: 1px solid var(--line-strong); border-radius: var(--radius-control); color: var(--text-primary); }
.version-editor select { min-height: 2.4rem; padding: 0 0.65rem; }
.version-editor__toolbar { align-items: center; background: var(--surface-panel); border-bottom: 1px solid var(--line); display: flex; gap: 1rem; justify-content: space-between; min-height: 3.5rem; padding: 0.5rem 1.4rem; }
.version-editor__toolbar > span { color: var(--text-secondary); font-size: 0.7rem; }
.version-editor__toolbar > div { display: flex; gap: 0.4rem; }
.version-editor__fields { display: grid; gap: 1rem; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); min-height: 0; overflow-y: auto; padding: 1.25rem 1.4rem; }
.version-editor__fields label { color: var(--text-secondary); display: grid; font-size: 0.7rem; font-weight: 650; gap: 0.4rem; }
.version-editor input { height: 2.6rem; min-height: 2.6rem; padding: 0 0.75rem; }
.version-editor textarea { font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.75rem; line-height: 1.55; min-height: 15rem; padding: 0.8rem; resize: vertical; }
.version-editor input[readonly], .version-editor textarea[readonly] { background: var(--surface-panel); color: var(--text-secondary); }
.version-editor__valid { align-items: center; background: var(--status-success-bg); color: var(--status-success-text) !important; display: flex; gap: 0.4rem; padding: 0.65rem 1.4rem; }
@media (max-width: 700px) { .version-editor > header, .version-editor__toolbar { align-items: stretch; flex-direction: column; } .version-editor__toolbar > div { flex-wrap: wrap; } .version-editor__fields { grid-template-columns: 1fr; } }
</style>
