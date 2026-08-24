import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it } from "vitest";

import type { AgentConfigurationClient } from "../src/agentConfiguration/agentConfigurationClient";
import { setAgentConfigurationClientFactoryForTests, useAgentConfigurationStore } from "../src/stores/agentConfiguration";

afterEach(() => setAgentConfigurationClientFactoryForTests(null));

describe("Agent configuration store", () => {
  it("creates a draft before editing a published version and binds only compatible published versions", async () => {
    const calls: string[] = [];
    setAgentConfigurationClientFactoryForTests(() => fakeClient(calls));
    setActivePinia(createPinia());
    const store = useAgentConfigurationStore();

    await store.initialize({ node: "conversation" });
    store.selectVersion("version_published");
    await store.beginEditingSelected();
    store.updateDraft({ content: "新的编排内容" });
    await store.saveDraft();
    await store.validateSelected();
    await store.publishSelected();
    await store.bindVersion("version_draft");

    expect(calls).toEqual([
      "createDraft:version_published",
      "updateDraft:version_draft",
      "validate:version_draft",
      "publish:version_draft",
      "bind:conversation:version_draft"
    ]);
    expect(store.dirty).toBe(false);
    expect(store.compatiblePublishedVersions.map((item) => item.id)).toContain("version_draft");
  });

  it("keeps unsaved content visible after a save failure", async () => {
    setAgentConfigurationClientFactoryForTests(() => ({
      ...fakeClient([]),
      updateDraft: async () => { throw new Error("offline"); }
    }));
    setActivePinia(createPinia());
    const store = useAgentConfigurationStore();
    await store.initialize({ node: "conversation" });
    store.selectVersion("version_draft");
    store.updateDraft({ content: "不能丢失的内容" });

    await expect(store.saveDraft()).rejects.toThrow("offline");
    expect(store.draft.content).toBe("不能丢失的内容");
    expect(store.dirty).toBe(true);
    expect(store.errorMessage).not.toBeNull();
  });
});

function fakeClient(calls: string[]): AgentConfigurationClient {
  const published = version("version_published", 1, "published");
  const draft = version("version_draft", 2, "draft");
  return {
    listLibrary: async () => ({
      resources: [resource()], versions: [published, draft], bindings: [], capabilities: { canManageConfiguration: true }
    }),
    listAudit: async () => ({ items: [], nextCursor: null }),
    createResource: async () => ({ resource: resource(), version: draft, capabilities: { canManageConfiguration: true } }),
    createDraft: async (_resourceId, request) => { calls.push(`createDraft:${request.sourceVersionId}`); return { resource: resource(), version: draft, capabilities: { canManageConfiguration: true } }; },
    updateDraft: async (versionId) => { calls.push(`updateDraft:${versionId}`); return { resource: resource(), version: draft, capabilities: { canManageConfiguration: true } }; },
    validateVersion: async (versionId) => { calls.push(`validate:${versionId}`); return { valid: true, warnings: [] }; },
    publishVersion: async (versionId) => { calls.push(`publish:${versionId}`); return { resource: resource(), version: { ...draft, status: "published", publishedAt: "2026-08-24T00:05:00Z" }, capabilities: { canManageConfiguration: true } }; },
    deprecateVersion: async () => ({ resource: resource(), version: published, capabilities: { canManageConfiguration: true } }),
    updateBinding: async (node, request) => { calls.push(`bind:${node}:${request.promptVersionId ?? request.skillVersionIds[0]}`); return { binding: { id: "binding_1", node, promptVersionId: request.promptVersionId, skillVersionIds: request.skillVersionIds, updatedAt: "2026-08-24T00:06:00Z" }, capabilities: { canManageConfiguration: true } }; }
  };
}

function resource() {
  return { id: "prompt_1", kind: "prompt" as const, name: "主编排", description: "只用于对话入口", createdAt: "2026-08-24T00:00:00Z", updatedAt: "2026-08-24T00:00:00Z" };
}

function version(id: string, number: number, status: "draft" | "published") {
  return { id, resourceId: "prompt_1", version: number, status, content: "编排内容", spec: { bindableNodes: ["conversation"] }, createdAt: "2026-08-24T00:00:00Z", publishedAt: status === "published" ? "2026-08-24T00:01:00Z" : null };
}
