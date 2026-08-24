import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it } from "vitest";

import type { IncidentSummary } from "@agent-py/api-contracts";

import type { IncidentClient } from "../src/incidents/incidentClient";
import { setIncidentClientFactoryForTests, useIncidentStore } from "../src/stores/incidents";

const incidents: readonly IncidentSummary[] = [
  incident({ id: "incident_critical", status: "active", severity: "critical" }),
  incident({
    id: "incident_approval",
    status: "active",
    severity: "high",
    approvalStatus: "pending",
    recoveryMode: "manual_review",
    recoveryExecutionStatus: "awaiting_approval",
    recoveryIntentId: "intent_approval",
    productionRecoveryExecution: true
  }),
  incident({ id: "incident_resolved", status: "resolved", severity: "low", currentStage: "closed" })
];

afterEach(() => setIncidentClientFactoryForTests(null));

describe("Incident store", () => {
  it("filters without mutating server data and diagnoses the real ID", async () => {
    const diagnosedIds: string[] = [];
    setIncidentClientFactoryForTests(() => fakeClient(diagnosedIds));
    setActivePinia(createPinia());
    const store = useIncidentStore();

    await store.initialize();
    store.setStatusFilter("active");
    store.setSeverityFilter("critical");

    expect(store.visibleIncidents.map((item) => item.id)).toEqual(["incident_critical"]);
    expect(store.items).toHaveLength(3);
    expect(store.selectedIncident?.id).toBe("incident_critical");
    expect(store.metrics.pendingApprovalCount).toBe(1);
    await store.startDiagnostic("incident_critical");
    expect(diagnosedIds).toEqual(["incident_critical"]);
  });

  it("retains an explicit error and supports retry", async () => {
    let attempts = 0;
    setIncidentClientFactoryForTests(() => ({
      diagnoseIncident: async () => ({ incidentId: "x", diagnosticTaskId: "d", backgroundJobId: "j", reused: false }),
      getIncident: async () => { throw new Error("unused"); },
      listIncidents: async () => {
        attempts += 1;
        if (attempts === 1) throw new Error("offline");
        return { items: incidents, nextCursor: null };
      }
    }));
    setActivePinia(createPinia());
    const store = useIncidentStore();

    await expect(store.initialize()).rejects.toThrow("offline");
    expect(store.errorMessage).not.toBeNull();
    await store.initialize();
    expect(store.errorMessage).toBeNull();
    expect(store.items).toHaveLength(3);
  });
});

function incident(overrides: Partial<IncidentSummary> = {}): IncidentSummary {
  return {
    id: "incident_1",
    status: "active",
    alertName: "OrderPoolExhausted",
    service: "order-service",
    severity: "medium",
    firstSeenAt: "2026-08-23T08:00:00Z",
    lastSeenAt: "2026-08-23T08:05:00Z",
    updatedAt: "2026-08-23T08:05:00Z",
    deliveryCount: 1,
    diagnosticTaskId: null,
    diagnosticStatus: null,
    verificationStatus: "not_available",
    currentStage: "alert",
    source: "local-alertmanager",
    environment: "test",
    assignee: null,
    agentMode: null,
    approvalStatus: null,
    recoveryMode: "not_available",
    recoveryExecutionStatus: "not_available",
    recoveryIntentId: null,
    productionRecoveryExecution: false,
    ...overrides
  };
}

function fakeClient(diagnosedIds: string[]): IncidentClient {
  return {
    listIncidents: async () => ({ items: incidents, nextCursor: null }),
    getIncident: async (incidentId) => ({ incident: { ...incident(), id: incidentId, summary: null, alertLabels: {}, alertAnnotations: {}, evidenceChain: null, recoveryIntent: null, recoveryEvents: [] } }),
    diagnoseIncident: async (incidentId) => {
      diagnosedIds.push(incidentId);
      return { incidentId, diagnosticTaskId: `diagnostic_${incidentId}`, backgroundJobId: `job_${incidentId}`, reused: false };
    }
  };
}
