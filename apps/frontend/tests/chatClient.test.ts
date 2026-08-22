import { describe, expect, it, vi } from "vitest";

import type { ChatRun, PendingChatAction } from "@agent-py/api-contracts";
import { createChatClient } from "../src/chat/chatClient";

const run: ChatRun = {
  id: "run_1",
  sessionId: "session_1",
  clientRequestId: "request_1",
  status: "queued",
  lastEventSequence: 0,
  errorCode: null,
  createdAt: "2026-08-22T00:00:00Z",
  updatedAt: "2026-08-22T00:00:00Z"
};

const pendingAction: PendingChatAction = {
  id: "chat_action_1",
  sessionId: "session_1",
  actionType: "start_diagnostic",
  targetResourceId: "incident_1",
  publicArguments: { incidentId: "incident_1" },
  status: "pending",
  expiresAt: "2026-08-22T00:15:00Z",
  backgroundJobId: null,
  executionResultId: null
};

describe("durable chat client", () => {
  it("uses the create, status, and active run endpoints", async () => {
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({
      ok: true,
      data: run,
      meta: { requestId: "request_test" }
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const client = createChatClient({
      baseUrl: "http://test",
      fetchImpl: fetchImpl as typeof fetch,
      getAccessToken: () => "token"
    });

    await client.createRun?.("session_1", { content: "diagnose", clientRequestId: "request_1" });
    await client.getRun?.("session_1", "run_1");
    await client.getActiveRun?.("session_1");

    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      "http://test/chat/sessions/session_1/runs",
      "http://test/chat/sessions/session_1/runs/run_1",
      "http://test/chat/sessions/session_1/runs/active"
    ]);
  });

  it("lists, confirms, and cancels pending actions", async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify({
      ok: true,
      data: String(input).endsWith("/pending") ? { items: [pendingAction] } : pendingAction,
      meta: { requestId: "request_test" }
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const client = createChatClient({
      baseUrl: "http://test",
      fetchImpl: fetchImpl as typeof fetch,
      getAccessToken: () => "token"
    });

    await client.listPendingActions?.("session_1");
    await client.confirmPendingAction?.("chat_action_1");
    await client.cancelPendingAction?.("chat_action_1");

    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      "http://test/chat/sessions/session_1/actions/pending",
      "http://test/chat/actions/chat_action_1/confirm",
      "http://test/chat/actions/chat_action_1/cancel"
    ]);
  });
});
