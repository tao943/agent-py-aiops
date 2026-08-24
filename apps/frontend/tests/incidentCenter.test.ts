// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it } from "vitest";

import type { IncidentSummary } from "@agent-py/api-contracts";

import IncidentCenterView from "../src/views/IncidentCenterView.vue";
import { setIncidentClientFactoryForTests } from "../src/stores/incidents";

afterEach(() => setIncidentClientFactoryForTests(null));

describe("Incident center", () => {
  it("renders real queue metrics and selected preview without synthetic safety data", async () => {
    const item = incident();
    setIncidentClientFactoryForTests(() => ({
      listIncidents: async () => ({ items: [item], nextCursor: null }),
      getIncident: async () => ({ incident: { ...item, summary: null, alertLabels: {}, alertAnnotations: {}, evidenceChain: null, recoveryIntent: null, recoveryEvents: [] } }),
      diagnoseIncident: async () => ({ incidentId: item.id, diagnosticTaskId: "diagnostic_1", backgroundJobId: "job_1", reused: false })
    }));
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/", component: { template: "<div />" } }]
    });
    const wrapper = mount(IncidentCenterView, {
      global: {
        plugins: [createPinia(), router],
        stubs: {
          RouterLink: { props: ["to"], template: '<a :href="typeof to === \'string\' ? to : to.path"><slot /></a>' },
          Teleport: true
        }
      }
    });
    await flushPromises();

    expect(wrapper.get('[aria-label="事件队列"]').text()).toContain("OrderPoolExhausted");
    expect(wrapper.get('[aria-label="事件预览"]').text()).toContain("incident_critical");
    expect(wrapper.text()).toContain("24 小时安全闭环率");
    expect(wrapper.text()).toContain("暂无数据");
    expect(wrapper.text()).toContain("自动恢复执行中");
    expect(wrapper.text()).toContain("未启用");
  });
});

function incident(): IncidentSummary {
  return {
    id: "incident_critical", status: "active", alertName: "OrderPoolExhausted",
    service: "order-service", severity: "critical", firstSeenAt: "2026-08-23T08:00:00Z",
    lastSeenAt: "2026-08-23T08:05:00Z", updatedAt: "2026-08-23T08:05:00Z",
    deliveryCount: 2, diagnosticTaskId: null, diagnosticStatus: null,
    verificationStatus: "not_available", currentStage: "alert", source: "local-alertmanager",
    environment: "test", assignee: null, agentMode: null, approvalStatus: null,
    recoveryMode: "not_available", recoveryExecutionStatus: "not_available",
    recoveryIntentId: null, productionRecoveryExecution: false
  };
}
