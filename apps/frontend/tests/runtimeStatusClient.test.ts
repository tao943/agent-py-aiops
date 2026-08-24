import { describe, expect, it, vi } from "vitest";
import { createRuntimeStatusClient } from "../src/runtime/runtimeStatusClient";

describe("runtime status client", () => {
  it("composes health, readiness, safe configuration and owner jobs", async () => {
    const fetchImpl = vi.fn(async (input: string | URL | Request) => {
      const path = String(input);
      const data = path.endsWith("/health") ? { service: "super-ai-backend", status: "ok", version: "0.1.0" }
        : path.endsWith("/ready") ? { status: "degraded", dependencies: { postgresql: { ok: true, latencyMs: 4 }, milvus: { ok: true }, llm: { ok: true }, mcp: { ok: true }, redis: { ok: false, error: "Redis is unavailable." } } }
          : path.endsWith("/config/check") ? { status: "degraded", configuration: { postgresql: { valid: true }, llm: { valid: true }, milvus: { valid: true }, mcp: { valid: true } }, dependencies: {} }
            : { items: [] };
      return new Response(JSON.stringify({ ok: true, data, meta: { requestId: "req" } }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const client = createRuntimeStatusClient({ baseUrl: "http://api.test", fetchImpl: fetchImpl as typeof fetch, getAccessToken: () => "token" });

    const snapshot = await client.load();

    expect(snapshot.process.status).toBe("ok");
    expect(snapshot.dependencies.find((item) => item.name === "redis")).toMatchObject({ status: "unavailable", blocking: false });
    expect(snapshot.configuration.every((item) => item.valid)).toBe(true);
    expect(fetchImpl.mock.calls.map((call) => call[0])).toEqual(["http://api.test/health", "http://api.test/ready", "http://api.test/config/check", "http://api.test/background-jobs", "http://api.test/evaluation/runs?limit=20"]);
    expect(JSON.stringify(snapshot)).not.toContain("Redis is unavailable");
  });
});
