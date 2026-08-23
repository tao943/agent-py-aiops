// @vitest-environment jsdom

import { createPinia, setActivePinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RecoveryAuditEvent, RecoveryIntent } from "@agent-py/api-contracts";

import type { RecoveryClient } from "../src/recovery/recoveryClient";
import { setRecoveryClientFactoryForTests, useRecoveryStore } from "../src/stores/recovery";

let visibility: DocumentVisibilityState;

beforeEach(() => {
  vi.useFakeTimers();
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
  setActivePinia(createPinia());
});

afterEach(() => {
  setRecoveryClientFactoryForTests(null);
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("Recovery store", () => {
  it.each([
    "recovered", "denied", "rejected", "expired", "cancelled",
    "verification_failed", "manual_intervention"
  ] as const)("does not poll terminal status %s", async (status) => {
    const getIntent = vi.fn().mockResolvedValue({ intent: intent(status) });
    setRecoveryClientFactoryForTests(() => fakeClient({ getIntent }));
    const store = useRecoveryStore();

    await store.start("intent_1", intent(status));
    await vi.advanceTimersByTimeAsync(4_000);

    expect(getIntent).toHaveBeenCalledTimes(1);
    store.stop();
  });

  it("polls a non-terminal formal intent every two seconds and appends durable events once", async () => {
    const getIntent = vi.fn().mockResolvedValueOnce({ intent: intent("queued") }).mockResolvedValueOnce({ intent: intent("verifying") });
    const listEvents = vi.fn().mockResolvedValueOnce({ items: [event(1)] }).mockResolvedValueOnce({ items: [event(2)] });
    setRecoveryClientFactoryForTests(() => fakeClient({ getIntent, listEvents }));
    const store = useRecoveryStore();

    await store.start("intent_1");
    await vi.advanceTimersByTimeAsync(2_000);

    expect(getIntent).toHaveBeenCalledTimes(2);
    expect(listEvents).toHaveBeenNthCalledWith(2, "intent_1", 1);
    expect(store.events.map((item) => item.sequence)).toEqual([1, 2]);
    store.stop();
  });

  it("pauses while hidden, refreshes immediately when visible, and stops at a terminal state", async () => {
    const getIntent = vi.fn().mockResolvedValueOnce({ intent: intent("queued") }).mockResolvedValueOnce({ intent: intent("recovered") });
    setRecoveryClientFactoryForTests(() => fakeClient({ getIntent }));
    const store = useRecoveryStore();
    await store.start("intent_1");

    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(4_000);
    expect(getIntent).toHaveBeenCalledTimes(1);

    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await Promise.resolve();
    expect(getIntent).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(4_000);
    expect(getIntent).toHaveBeenCalledTimes(2);
    store.stop();
  });

  it("retains the last successful intent and marks it stale after a transient refresh failure", async () => {
    const getIntent = vi.fn().mockResolvedValueOnce({ intent: intent("queued") }).mockRejectedValueOnce(new Error("timeout"));
    setRecoveryClientFactoryForTests(() => fakeClient({ getIntent }));
    const store = useRecoveryStore();
    await store.start("intent_1");

    await vi.advanceTimersByTimeAsync(2_000);

    expect(store.intent?.status).toBe("queued");
    expect(store.stale).toBe(true);
    expect(store.errorMessage).not.toBeNull();
    store.stop();
  });

  it("does not restart polling when a pending refresh completes after disposal", async () => {
    let resolveIntent!: (value: { intent: RecoveryIntent }) => void;
    const getIntent = vi.fn(() => new Promise<{ intent: RecoveryIntent }>((resolve) => {
      resolveIntent = resolve;
    }));
    setRecoveryClientFactoryForTests(() => fakeClient({ getIntent }));
    const store = useRecoveryStore();

    const starting = store.start("intent_1", intent("queued"));
    store.stop();
    resolveIntent({ intent: intent("queued") });
    await starting;
    await vi.advanceTimersByTimeAsync(4_000);

    expect(getIntent).toHaveBeenCalledTimes(1);
  });
});

function fakeClient(overrides: Partial<RecoveryClient> = {}): RecoveryClient {
  return {
    createIntent: async () => ({ intent: intent("queued") }),
    getIntent: async () => ({ intent: intent("queued") }),
    listEvents: async () => ({ items: [] }),
    approveIntent: async () => ({ intent: intent("queued") }),
    rejectIntent: async () => ({ intent: intent("rejected") }),
    cancelIntent: async () => ({ intent: intent("cancelled") }),
    ...overrides
  };
}

function intent(status: RecoveryIntent["status"]): RecoveryIntent {
  return {
    id: "intent_1", incidentId: "incident_1", diagnosticTaskId: "diagnostic_1",
    reportId: "report_1", action: "restart_compose_service", targetKey: "order-service",
    riskTier: "low", automaticEligible: true, approvalRequired: false, status,
    proposalFingerprint: "sha256:proposal", createdAt: "2026-08-23T08:00:00Z",
    approvalExpiresAt: null, startedAt: null, completedAt: null,
    safeReasonCode: null, executionSummary: null, verification: []
  };
}

function event(sequence: number): RecoveryAuditEvent {
  return {
    sequence, type: "recovery.status", fromStatus: "queued", toStatus: "revalidating",
    safeReasonCode: null, safeSummary: `安全事件 ${sequence}`, durationMs: null,
    createdAt: "2026-08-23T08:00:01Z"
  };
}
