// @vitest-environment jsdom

import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  AiopsDiagnosticEvidenceChain,
  IncidentDetail,
  RecoveryIntent
} from "@agent-py/api-contracts";

import IncidentWorkspaceView from "../src/views/IncidentWorkspaceView.vue";
import { setIncidentClientFactoryForTests } from "../src/stores/incidents";
import { setRecoveryClientFactoryForTests } from "../src/stores/recovery";

afterEach(() => {
  setIncidentClientFactoryForTests(null);
  setRecoveryClientFactoryForTests(null);
});

describe("Investigation workspace", () => {
  it("presents multi-agent degradation, validator fallback, and forbidden execution without hidden reasoning", async () => {
    const wrapper = await mountWorkspace(detail({
      agentMode: "multi",
      evidenceChain: chain({
        agentMode: "multi",
        specialistResults: [
          { role: "runtime", status: "supported", safeSummary: "运行时证据完整" },
          { role: "log", status: "inconclusive", safeSummary: "缺少独立日志证据" }
        ],
        validation: { validationOrigin: "deterministic_grounded_fallback" },
        recoveryPolicy: { executionPermitted: false }
      })
    }));

    expect(wrapper.get('[data-status="inconclusive"]').text()).toContain("缺少独立日志证据");
    expect(wrapper.get('[data-validator="deterministic_grounded_fallback"]').text())
      .toContain("语义核验不可用，确定性证据通过，已转人工复核");
    expect(wrapper.get('[data-recovery-permitted="false"]').text()).toContain("禁止自动执行");
    expect(wrapper.text()).not.toMatch(/checkpointPayload|rawResponse|隐藏推理/);
  });

  it("renders six investigation tabs and never offers an execute button for automatic Compose recovery", async () => {
    const recovery = intent({ status: "executing", action: "restart_compose_service" });
    const wrapper = await mountWorkspace(detail({ recoveryIntent: recovery, recoveryIntentId: recovery.id }));

    expect(wrapper.get('[role="tablist"]').text()).toContain("概览");
    expect(wrapper.get('[role="tablist"]').text()).toContain("执行链");
    expect(wrapper.get('[role="tablist"]').text()).toContain("假设与证据");
    expect(wrapper.get('[role="tablist"]').text()).toContain("工具审计");
    expect(wrapper.get('[role="tablist"]').text()).toContain("恢复闭环");
    expect(wrapper.get('[role="tablist"]').text()).toContain("审计时间线");

    await wrapper.get('#tab-recovery').trigger("click");
    expect(wrapper.text()).toContain("自动恢复由受控执行器推进");
    expect(wrapper.find('button[data-action="execute"]').exists()).toBe(false);
  });

  it("approves PostgreSQL recovery using only an incident-bound confirmation", async () => {
    const approveIntent = vi.fn(async () => ({ intent: intent({ status: "queued" }) }));
    const recovery = intent({ status: "awaiting_approval", action: "terminate_postgres_blocker" });
    const wrapper = await mountWorkspace(
      detail({ recoveryIntent: recovery, recoveryIntentId: recovery.id, approvalStatus: "pending" }),
      { approveIntent }
    );

    await wrapper.get('#tab-recovery').trigger("click");
    expect(wrapper.text()).toContain("高风险数据库恢复");
    await wrapper.get('[data-action="approve"]').trigger("click");
    await flushPromises();

    expect(approveIntent).toHaveBeenCalledWith("intent_1", "incident_1");
  });

  it.each(["verification_failed", "manual_intervention"] as const)(
    "keeps %s recovery read-only without an automatic retry action",
    async (status) => {
      const recovery = intent({ status });
      const wrapper = await mountWorkspace(detail({ recoveryIntent: recovery, recoveryIntentId: recovery.id }));

      await wrapper.get('#tab-recovery').trigger("click");

      expect(wrapper.text()).toContain(status === "verification_failed" ? "独立验证未通过" : "需要人工介入");
      expect(wrapper.find('[data-action="execute"]').exists()).toBe(false);
      expect(wrapper.find('[data-action="retry-recovery"]').exists()).toBe(false);
    }
  );
});

async function mountWorkspace(
  incident: IncidentDetail,
  recoveryOverrides: Record<string, unknown> = {}
) {
  setIncidentClientFactoryForTests(() => ({
    listIncidents: async () => ({ items: [incident], nextCursor: null }),
    getIncident: async () => ({ incident }),
    diagnoseIncident: async () => ({ incidentId: incident.id, diagnosticTaskId: "diagnostic_1", backgroundJobId: "job_1", reused: false })
  }));
  setRecoveryClientFactoryForTests(() => ({
    createIntent: async () => ({ intent: intent() }),
    getIntent: async () => ({ intent: incident.recoveryIntent ?? intent() }),
    listEvents: async () => ({ items: incident.recoveryEvents }),
    approveIntent: async () => ({ intent: intent({ status: "queued" }) }),
    rejectIntent: async () => ({ intent: intent({ status: "rejected" }) }),
    cancelIntent: async () => ({ intent: intent({ status: "cancelled" }) }),
    ...recoveryOverrides
  }));
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/incidents/:incidentId", component: IncidentWorkspaceView }]
  });
  await router.push(`/incidents/${incident.id}`);
  await router.isReady();
  const wrapper = mount(IncidentWorkspaceView, { global: { plugins: [createPinia(), router] } });
  await flushPromises();
  return wrapper;
}

function detail(overrides: Partial<IncidentDetail> = {}): IncidentDetail {
  return {
    id: "incident_1", status: "active", alertName: "OrderPoolExhausted",
    service: "order-service", severity: "critical", firstSeenAt: "2026-08-23T08:00:00Z",
    lastSeenAt: "2026-08-23T08:05:00Z", updatedAt: "2026-08-23T08:05:00Z",
    deliveryCount: 2, diagnosticTaskId: "diagnostic_1", diagnosticStatus: "succeeded",
    verificationStatus: "pending", currentStage: "decision", source: "alertmanager",
    environment: "test", assignee: null, agentMode: "single", approvalStatus: null,
    recoveryMode: "manual_review", recoveryExecutionStatus: "not_available",
    recoveryIntentId: null, productionRecoveryExecution: false,
    summary: "订单连接池持续耗尽", alertLabels: { region: "ap-shanghai" },
    alertAnnotations: {}, evidenceChain: chain({ recoveryPolicy: { executionPermitted: false } }),
    recoveryIntent: null, recoveryEvents: [], ...overrides
  };
}

function chain(resultPayload: Record<string, unknown>): AiopsDiagnosticEvidenceChain {
  const task = {
    id: "diagnostic_1", ownerUserId: "user_1", status: "succeeded" as const,
    query: "排查订单连接池", inputPayload: {}, resultPayload,
    createdAt: "2026-08-23T08:00:00Z", updatedAt: "2026-08-23T08:02:00Z",
    completedAt: "2026-08-23T08:02:00Z", reports: []
  };
  return { task, steps: [], toolCalls: [], evidence: [], reports: [], reportEvidenceLinks: [], checkpoints: [] };
}

function intent(overrides: Partial<RecoveryIntent> = {}): RecoveryIntent {
  return {
    id: "intent_1", incidentId: "incident_1", diagnosticTaskId: "diagnostic_1",
    reportId: "report_1", action: "restart_compose_service", targetKey: "order-service",
    riskTier: "low", automaticEligible: true, approvalRequired: false, status: "queued",
    proposalFingerprint: "sha256:proposal", createdAt: "2026-08-23T08:03:00Z",
    approvalExpiresAt: null, startedAt: null, completedAt: null, safeReasonCode: null,
    executionSummary: null, verification: [], ...overrides
  };
}
