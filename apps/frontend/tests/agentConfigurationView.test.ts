// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it } from "vitest";

import type { AgentConfigurationClient } from "../src/agentConfiguration/agentConfigurationClient";
import { setAgentConfigurationClientFactoryForTests } from "../src/stores/agentConfiguration";
import AgentConfigurationView from "../src/views/AgentConfigurationView.vue";

afterEach(() => setAgentConfigurationClientFactoryForTests(null));

describe("Agent configuration view", () => {
  it("renders the version workflow and rebinds a historical published version", async () => {
    const bindings: string[] = [];
    setAgentConfigurationClientFactoryForTests(() => fakeClient(bindings));
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/agent-config", component: AgentConfigurationView }]
    });
    await router.push("/agent-config?node=conversation");
    await router.isReady();
    const wrapper = mount(AgentConfigurationView, { global: { plugins: [createPinia(), router], stubs: { Teleport: true } } });
    await flushPromises();

    expect(wrapper.text()).toContain("配置控制台");
    expect(wrapper.text()).toContain("主编排 Prompt");
    expect(wrapper.get("textarea").attributes("readonly")).toBeDefined();
    await wrapper.get('[aria-label="重新绑定版本 1"]').trigger("click");
    await flushPromises();
    expect(bindings).toEqual(["version_1"]);
  });
});

function fakeClient(bindings: string[]): AgentConfigurationClient {
  const resource = { id: "prompt_1", kind: "prompt" as const, name: "主编排 Prompt", description: "对话入口路由", createdAt: "2026-08-24T00:00:00Z", updatedAt: "2026-08-24T00:00:00Z" };
  const version = { id: "version_1", resourceId: resource.id, version: 1, status: "published" as const, content: "先确认事件，再进入调查。", spec: { bindableNodes: ["conversation"] }, createdAt: "2026-08-24T00:00:00Z", publishedAt: "2026-08-24T00:01:00Z" };
  return {
    listLibrary: async () => ({ resources: [resource], versions: [version], bindings: [{ id: "binding_1", node: "conversation", promptVersionId: null, skillVersionIds: [], updatedAt: "2026-08-24T00:02:00Z" }], capabilities: { canManageConfiguration: true } }),
    listAudit: async () => ({ items: [{ id: "audit_1", resourceId: resource.id, versionId: version.id, bindingId: "binding_1", action: "binding_updated", actorUserId: "owner", safeSummary: "binding.updated", createdAt: "2026-08-24T00:02:00Z" }], nextCursor: null }),
    createResource: async () => ({ resource, version, capabilities: { canManageConfiguration: true } }),
    createDraft: async () => ({ resource, version, capabilities: { canManageConfiguration: true } }),
    updateDraft: async () => ({ resource, version, capabilities: { canManageConfiguration: true } }),
    validateVersion: async () => ({ valid: true, warnings: [] }),
    publishVersion: async () => ({ resource, version, capabilities: { canManageConfiguration: true } }),
    deprecateVersion: async () => ({ resource, version: { ...version, status: "deprecated" }, capabilities: { canManageConfiguration: true } }),
    updateBinding: async (_node, request) => { bindings.push(request.promptVersionId ?? ""); return { binding: { id: "binding_1", node: "conversation", promptVersionId: request.promptVersionId, skillVersionIds: request.skillVersionIds, updatedAt: "2026-08-24T00:03:00Z" }, capabilities: { canManageConfiguration: true } }; }
  };
}
