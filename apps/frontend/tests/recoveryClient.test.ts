import { describe, expect, it, vi } from "vitest";

import type { RecoveryAuditEvent, RecoveryIntent } from "@agent-py/api-contracts";

import { createRecoveryClient } from "../src/recovery/recoveryClient";

describe("Recovery client", () => {
  it("uses owner-scoped formal intent endpoints and sends only the incident confirmation", async () => {
    const requests: Array<{ url: string; init: RequestInit | undefined }> = [];
    const fetchImpl = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      requests.push({ url: String(input), init });
      return response(intent());
    });
    const client = createRecoveryClient({
      baseUrl: "http://api.test",
      fetchImpl: fetchImpl as typeof fetch,
      getAccessToken: () => "token"
    });

    await client.approveIntent("intent/unsafe", "incident_1");

    expect(requests[0]?.url).toBe("http://api.test/aiops/recovery-intents/intent%2Funsafe:approve");
    expect(JSON.parse(String(requests[0]?.init?.body))).toEqual({
      incidentIdConfirmation: "incident_1"
    });
    expect(String(requests[0]?.init?.body)).not.toMatch(/action|target|pid|sql|path|arguments/i);
  });

  it("uses the last durable event sequence as an opaque incremental cursor", async () => {
    const fetchImpl = vi.fn(async () => response({ items: [event(8)] }));
    const client = createRecoveryClient({
      baseUrl: "http://api.test",
      fetchImpl: fetchImpl as typeof fetch,
      getAccessToken: () => null
    });

    await client.listEvents("intent_1", 7);

    expect(fetchImpl).toHaveBeenCalledWith(
      "http://api.test/aiops/recovery-intents/intent_1/events?afterSequence=7",
      expect.any(Object)
    );
  });
});

function response<T>(data: T): Response {
  return new Response(JSON.stringify({ ok: true, data, meta: { requestId: "request_1" } }), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
}

function intent(): RecoveryIntent {
  return {
    id: "intent_1", incidentId: "incident_1", diagnosticTaskId: "diagnostic_1",
    reportId: "report_1", action: "terminate_postgres_blocker", targetKey: "orders-db",
    riskTier: "high", automaticEligible: false, approvalRequired: true,
    status: "awaiting_approval", proposalFingerprint: "sha256:proposal",
    createdAt: "2026-08-23T08:00:00Z", approvalExpiresAt: "2026-08-23T08:15:00Z",
    startedAt: null, completedAt: null, safeReasonCode: null,
    executionSummary: null, verification: []
  };
}

function event(sequence: number): RecoveryAuditEvent {
  return {
    sequence, type: "recovery.status", fromStatus: "queued", toStatus: "revalidating",
    safeReasonCode: null, safeSummary: "已开始安全复核", durationMs: 12,
    createdAt: "2026-08-23T08:00:01Z"
  };
}
