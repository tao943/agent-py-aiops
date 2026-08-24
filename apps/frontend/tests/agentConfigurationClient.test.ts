import { describe, expect, it, vi } from "vitest";

import { createAgentConfigurationClient } from "../src/agentConfiguration/agentConfigurationClient";

describe("Agent configuration client", () => {
  it("uses version lifecycle, binding, and audit endpoints", async () => {
    const fetchImpl = vi.fn(async (input: string | URL | Request, _init?: RequestInit) => {
      const path = String(input);
      const data = path.endsWith("/resources")
        ? { resources: [], versions: [], bindings: [], capabilities: { canManageConfiguration: true } }
        : path.endsWith("/audit")
          ? { items: [], nextCursor: null }
          : path.includes("/bindings/")
            ? { binding: { id: "binding_1", node: "conversation", promptVersionId: null, skillVersionIds: [], updatedAt: "2026-08-24T00:00:00Z" }, capabilities: { canManageConfiguration: true } }
            : path.endsWith(":validate")
              ? { valid: true, warnings: [] }
              : { resource: resource(), version: version(), capabilities: { canManageConfiguration: true } };
      return new Response(JSON.stringify({ ok: true, data, meta: { requestId: "req_1" } }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      });
    });
    const client = createAgentConfigurationClient({
      baseUrl: "http://api.test",
      fetchImpl: fetchImpl as typeof fetch,
      getAccessToken: () => "token"
    });

    await client.listLibrary();
    await client.validateVersion("version_1");
    await client.publishVersion("version_1");
    await client.updateBinding("conversation", { promptVersionId: "version_1", skillVersionIds: [] });
    await client.listAudit();

    expect(fetchImpl.mock.calls.map((call) => call[0])).toEqual([
      "http://api.test/agent-configuration/resources",
      "http://api.test/agent-configuration/versions/version_1:validate",
      "http://api.test/agent-configuration/versions/version_1:publish",
      "http://api.test/agent-configuration/bindings/conversation",
      "http://api.test/agent-configuration/audit"
    ]);
    expect(fetchImpl.mock.calls[3]?.[1]).toMatchObject({ method: "PUT" });
  });
});

function resource() {
  return { id: "prompt_1", kind: "prompt" as const, name: "主编排", description: null, createdAt: "2026-08-24T00:00:00Z", updatedAt: "2026-08-24T00:00:00Z" };
}

function version() {
  return { id: "version_1", resourceId: "prompt_1", version: 1, status: "draft" as const, content: "内容", spec: { bindableNodes: ["conversation"] }, createdAt: "2026-08-24T00:00:00Z", publishedAt: null };
}
