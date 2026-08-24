import { computed, ref } from "vue";
import { defineStore } from "pinia";

import type {
  AgentBinding,
  AgentConfigurationAuditEvent,
  AgentNode,
  AgentResource,
  AgentResourceKind,
  AgentResourceVersion
} from "@agent-py/api-contracts";

import {
  createAgentConfigurationClient,
  type AgentConfigurationClient,
  type AgentVersionValidationResponse
} from "../agentConfiguration/agentConfigurationClient";
import { toUserFacingError } from "../ui/userFacingError";

export interface AgentDraftEditor {
  readonly name: string;
  readonly description: string;
  readonly content: string;
  readonly spec: Readonly<Record<string, unknown>>;
}

let clientFactory: () => AgentConfigurationClient = createAgentConfigurationClient;

export function setAgentConfigurationClientFactoryForTests(
  factory: (() => AgentConfigurationClient) | null
): void {
  clientFactory = factory ?? createAgentConfigurationClient;
}

export const useAgentConfigurationStore = defineStore("agent-configuration", () => {
  const client = clientFactory();
  const resources = ref<readonly AgentResource[]>([]);
  const versions = ref<readonly AgentResourceVersion[]>([]);
  const bindings = ref<readonly AgentBinding[]>([]);
  const auditEvents = ref<readonly AgentConfigurationAuditEvent[]>([]);
  const selectedNode = ref<AgentNode>("conversation");
  const selectedResourceId = ref<string | null>(null);
  const selectedVersionId = ref<string | null>(null);
  const draft = ref<AgentDraftEditor>(emptyDraft());
  const dirty = ref(false);
  const validation = ref<AgentVersionValidationResponse | null>(null);
  const canManageConfiguration = ref(false);
  const isLoading = ref(false);
  const isSaving = ref(false);
  const isPublishing = ref(false);
  const isBinding = ref(false);
  const errorMessage = ref<string | null>(null);

  const selectedResource = computed(() =>
    resources.value.find((item) => item.id === selectedResourceId.value) ?? null
  );
  const selectedVersion = computed(() =>
    versions.value.find((item) => item.id === selectedVersionId.value) ?? null
  );
  const selectedBinding = computed(() =>
    bindings.value.find((item) => item.node === selectedNode.value) ?? null
  );
  const resourceVersions = computed(() => versions.value
    .filter((item) => item.resourceId === selectedResourceId.value)
    .sort((left, right) => right.version - left.version));
  const compatiblePublishedVersions = computed(() => versions.value.filter((item) => {
    if (item.status !== "published") return false;
    const bindableNodes = readStringArray(item.spec.bindableNodes);
    return bindableNodes.includes(selectedNode.value);
  }));

  function report(error: unknown): void {
    errorMessage.value = toUserFacingError(error);
  }

  function selectResource(resourceId: string): void {
    selectedResourceId.value = resourceId;
    const latest = versions.value
      .filter((item) => item.resourceId === resourceId)
      .sort((left, right) => right.version - left.version)[0];
    selectVersion(latest?.id ?? null);
  }

  function selectVersion(versionId: string | null): void {
    selectedVersionId.value = versionId;
    const version = versions.value.find((item) => item.id === versionId);
    const resource = resources.value.find((item) => item.id === version?.resourceId);
    draft.value = version === undefined || resource === undefined
      ? emptyDraft()
      : { name: resource.name, description: resource.description ?? "", content: version.content, spec: version.spec };
    dirty.value = false;
    validation.value = null;
  }

  function upsertMutation(resource: AgentResource, version: AgentResourceVersion): void {
    resources.value = [resource, ...resources.value.filter((item) => item.id !== resource.id)];
    versions.value = [version, ...versions.value.filter((item) => item.id !== version.id)];
    selectedResourceId.value = resource.id;
    selectVersion(version.id);
  }

  async function initialize(options: { readonly node?: AgentNode } = {}): Promise<void> {
    isLoading.value = true;
    errorMessage.value = null;
    try {
      if (options.node !== undefined) selectedNode.value = options.node;
      const [library, audit] = await Promise.all([client.listLibrary(), client.listAudit()]);
      resources.value = library.resources;
      versions.value = library.versions;
      bindings.value = library.bindings;
      auditEvents.value = audit.items;
      canManageConfiguration.value = library.capabilities.canManageConfiguration;
      const bound = bindings.value.find((item) => item.node === selectedNode.value);
      const boundVersionId = bound?.promptVersionId ?? bound?.skillVersionIds[0] ?? null;
      const boundVersion = versions.value.find((item) => item.id === boundVersionId);
      const firstResource = resources.value.find((item) => item.id === boundVersion?.resourceId) ?? resources.value[0];
      if (firstResource !== undefined) selectResource(firstResource.id);
    } catch (error) {
      report(error);
      throw error;
    } finally {
      isLoading.value = false;
    }
  }

  async function beginEditingSelected(): Promise<void> {
    const version = selectedVersion.value;
    const resource = selectedResource.value;
    if (version === null || resource === null || version.status === "draft") return;
    isSaving.value = true;
    try {
      const result = await client.createDraft(resource.id, {
        content: version.content,
        spec: version.spec,
        sourceVersionId: version.id
      });
      upsertMutation(result.resource, result.version);
    } catch (error) {
      report(error);
      throw error;
    } finally {
      isSaving.value = false;
    }
  }

  function updateDraft(patch: Partial<AgentDraftEditor>): void {
    draft.value = { ...draft.value, ...patch };
    dirty.value = true;
    validation.value = null;
  }

  async function saveDraft(): Promise<void> {
    const version = selectedVersion.value;
    if (version === null || version.status !== "draft") return;
    isSaving.value = true;
    errorMessage.value = null;
    try {
      const result = await client.updateDraft(version.id, {
        name: draft.value.name,
        description: draft.value.description,
        content: draft.value.content,
        spec: draft.value.spec
      });
      upsertMutation(result.resource, result.version);
    } catch (error) {
      report(error);
      throw error;
    } finally {
      isSaving.value = false;
    }
  }

  async function validateSelected(): Promise<AgentVersionValidationResponse | null> {
    if (selectedVersion.value === null) return null;
    try {
      validation.value = await client.validateVersion(selectedVersion.value.id);
      return validation.value;
    } catch (error) {
      report(error);
      throw error;
    }
  }

  async function publishSelected(): Promise<void> {
    const version = selectedVersion.value;
    if (version === null || version.status !== "draft" || validation.value?.valid !== true) return;
    isPublishing.value = true;
    try {
      const result = await client.publishVersion(version.id);
      upsertMutation(result.resource, result.version);
      await refreshAudit();
    } catch (error) {
      report(error);
      throw error;
    } finally {
      isPublishing.value = false;
    }
  }

  async function bindVersion(versionId: string): Promise<void> {
    const version = versions.value.find((item) => item.id === versionId);
    const resource = resources.value.find((item) => item.id === version?.resourceId);
    if (version?.status !== "published" || resource === undefined) return;
    if (!readStringArray(version.spec.bindableNodes).includes(selectedNode.value)) return;
    isBinding.value = true;
    try {
      const current = selectedBinding.value;
      const request = resource.kind === "prompt"
        ? { promptVersionId: version.id, skillVersionIds: current?.skillVersionIds ?? [] }
        : { promptVersionId: current?.promptVersionId ?? null, skillVersionIds: [version.id] };
      const result = await client.updateBinding(selectedNode.value, request);
      bindings.value = [result.binding, ...bindings.value.filter((item) => item.node !== selectedNode.value)];
      await refreshAudit();
    } catch (error) {
      report(error);
      throw error;
    } finally {
      isBinding.value = false;
    }
  }

  async function deprecateSelected(): Promise<void> {
    const version = selectedVersion.value;
    if (version === null || version.status !== "published") return;
    const result = await client.deprecateVersion(version.id);
    upsertMutation(result.resource, result.version);
    await refreshAudit();
  }

  async function createResource(kind: AgentResourceKind): Promise<void> {
    const name = kind === "prompt" ? "未命名 Prompt" : "未命名 Skill";
    const result = await client.createResource({
      kind, name, description: "", content: kind === "prompt" ? "请描述编排目标。" : "# Skill\n\n定义多步骤工作流。",
      spec: kind === "skill" ? defaultSkillSpec() : { bindableNodes: ["conversation"] }
    });
    upsertMutation(result.resource, result.version);
  }

  async function refreshAudit(): Promise<void> {
    auditEvents.value = (await client.listAudit()).items;
  }

  return {
    resources, versions, bindings, auditEvents, selectedNode, selectedResourceId, selectedVersionId,
    draft, dirty, validation, canManageConfiguration, isLoading, isSaving, isPublishing, isBinding,
    errorMessage, selectedResource, selectedVersion, selectedBinding, resourceVersions,
    compatiblePublishedVersions, initialize, selectResource, selectVersion, beginEditingSelected,
    updateDraft, saveDraft, validateSelected, publishSelected, bindVersion, deprecateSelected,
    createResource
  };
});

function emptyDraft(): AgentDraftEditor {
  return { name: "", description: "", content: "", spec: {} };
}

function readStringArray(value: unknown): readonly string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function defaultSkillSpec(): Readonly<Record<string, unknown>> {
  return {
    bindableNodes: ["conversation"], allowedTools: [], risk: "read_only", inputSchema: {},
    outputSchema: {}, timeoutMs: 30000, maxToolCalls: 8, retryPolicy: "safe_read_only",
    requiresApprovalFor: [], completionCriteria: []
  };
}
