// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, describe, expect, it } from "vitest";
import type { RuntimeStatusClient } from "../src/runtime/runtimeStatusClient";
import { setRuntimeStatusClientFactoryForTests } from "../src/stores/runtimeStatus";
import SystemStatusView from "../src/views/SystemStatusView.vue";

afterEach(() => setRuntimeStatusClientFactoryForTests(null));

describe("system status view", () => {
  it("separates process liveness from dependency degradation without leaking secrets", async () => {
    setRuntimeStatusClientFactoryForTests(() => fakeClient());
    const wrapper = mount(SystemStatusView, { global: { plugins: [createPinia()] } });
    await flushPromises();

    expect(wrapper.get('[data-capability="api-process"]').text()).toContain("进程在线");
    expect(wrapper.get('[data-capability="full-runtime"]').text()).toContain("依赖降级");
    expect(wrapper.text()).toContain("Redis");
    expect(wrapper.text()).toContain("非阻塞");
    expect(wrapper.text()).toContain("任务执行失败");
    expect(wrapper.get('[data-eval="live"]').text()).toContain("暂无已保存结果");
    expect(wrapper.text()).not.toContain("sk-test-secret");
    expect(wrapper.text()).not.toContain("postgresql://");
  });
});

function fakeClient(): RuntimeStatusClient {
  return { load: async () => ({
    process: { service: "super-ai-backend", status: "ok", version: "0.1.0" }, readinessStatus: "degraded", checkedAt: "2026-08-24T00:00:00Z",
    dependencies: [{ name: "postgresql", status: "ready", blocking: true, latencyMs: 4, safeSummary: "可用" }, { name: "redis", status: "unavailable", blocking: false, latencyMs: null, safeSummary: "不可用，缓存和分布式加速降级" }],
    configuration: [{ name: "llm", valid: true, safeSummary: "配置有效" }],
    jobs: [{ id: "job_1", ownerUserId: "owner", kind: "aiops_diagnosis", resourceType: "diagnostic", resourceId: "d1", status: "failed", attempt: 2, maxAttempts: 2, timeoutSeconds: 30, availableAt: "2026-08-24T00:00:00Z", cancelRequestedAt: null, retryOfJobId: null, errorMessage: "sk-test-secret postgresql://private", createdAt: "2026-08-24T00:00:00Z", updatedAt: "2026-08-24T00:00:00Z", startedAt: null, completedAt: null }],
    evaluations: []
  }) };
}
