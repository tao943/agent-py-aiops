import { createPinia, setActivePinia } from "pinia";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  ChatMessage,
  PendingChatAction,
  ChatRun,
  ChatSessionSummary,
  SseEvent,
  ToolCallAudit
} from "@agent-py/api-contracts";

import { setChatClientFactoryForTests, useChatStore } from "../src/stores/chat";
import type { ChatClient } from "../src/chat/chatClient";

const session = (overrides: Partial<ChatSessionSummary> = {}): ChatSessionSummary => ({
  id: "chat_1",
  ownerUserId: "user_1",
  title: "Restart API",
  createdAt: "2026-07-10T00:00:00.000Z",
  updatedAt: "2026-07-10T00:01:00.000Z",
  memory: {
    mode: "adaptive",
    summaryVersion: 0,
    compactionStatus: "idle",
    contextTokens: 1200,
    contextWindowTokens: 131072,
    contextUsagePercent: 0.9,
    compactedMessageCount: 0,
    lastCompactedAt: null,
    canCompact: true
  },
  ...overrides
});

const message = (overrides: Partial<ChatMessage> = {}): ChatMessage => ({
  id: "message_1",
  ownerUserId: "user_1",
  sessionId: "chat_1",
  role: "assistant",
  content: "Use the **runbook**.",
  metadata: {},
  createdAt: "2026-07-10T00:01:00.000Z",
  ...overrides
});

const audit = (): ToolCallAudit => ({
  id: "tool_1",
  ownerUserId: "user_1",
  sessionId: "chat_1",
  diagnosticTaskId: null,
  toolName: "knowledge_retrieval",
  status: "completed",
  arguments: { query: "restart api" },
  resultSummary: "Found 1 relevant chunk.",
  errorMessage: null,
  startedAt: "2026-07-10T00:00:01.000Z",
  completedAt: "2026-07-10T00:00:02.000Z",
  durationMs: 18,
  createdAt: "2026-07-10T00:00:01.000Z"
});

const pendingAction = (overrides: Partial<PendingChatAction> = {}): PendingChatAction => ({
  id: "chat_action_1",
  sessionId: "chat_1",
  actionType: "start_diagnostic",
  targetResourceId: "incident_1",
  publicArguments: {},
  status: "pending",
  expiresAt: "2026-08-22T00:15:00Z",
  backgroundJobId: null,
  executionResultId: null,
  ...overrides
});

afterEach(() => {
  vi.useRealTimers();
  setChatClientFactoryForTests(null);
});

describe("chat store", () => {
  it("loads session history from the backend when a user selects a conversation", async () => {
    const client = fakeClient();
    client.listPendingActions = async () => ({ items: [pendingAction()] });
    setChatClientFactoryForTests(() => client);
    setActivePinia(createPinia());
    const store = useChatStore();

    await store.initialize();
    await store.selectSession("chat_1");

    expect(store.sessions).toEqual([session()]);
    expect(store.activeSessionId).toBe("chat_1");
    expect(store.messages).toEqual([message()]);
    expect(store.toolAudits).toEqual([audit()]);
    expect(store.pendingActions).toEqual([pendingAction()]);
  });

  it("normalizes one-release legacy memory modes from an older API", async () => {
    const client = fakeClient();
    const legacy = session({
      memory: { ...session().memory, mode: "every_30_turns" as never }
    });
    client.listSessions = async () => ({ items: [legacy] });
    client.getSession = async () => ({ session: legacy, messages: [] });
    setChatClientFactoryForTests(() => client);
    setActivePinia(createPinia());
    const store = useChatStore();

    await store.initialize();

    expect(store.activeSession?.memory.mode).toBe("adaptive");
  });

  it("guards duplicate pending-action decisions while the first request is in flight", async () => {
    let releaseConfirmation: (() => void) | undefined;
    const confirmationGate = new Promise<void>((resolve) => {
      releaseConfirmation = resolve;
    });
    const confirmPendingAction = vi.fn(async () => {
      await confirmationGate;
      return pendingAction({ status: "confirmed", backgroundJobId: "job_1" });
    });
    const client = fakeClient();
    client.listPendingActions = async () => ({ items: [pendingAction()] });
    client.confirmPendingAction = confirmPendingAction;
    setChatClientFactoryForTests(() => client);
    setActivePinia(createPinia());
    const store = useChatStore();
    await store.initialize();

    const first = store.confirmPendingAction("chat_action_1");
    const duplicate = store.confirmPendingAction("chat_action_1");
    await Promise.resolve();

    expect(confirmPendingAction).toHaveBeenCalledTimes(1);
    expect(store.pendingActionLoadingIds).toEqual(["chat_action_1"]);
    releaseConfirmation?.();
    await Promise.all([first, duplicate]);
    expect(store.pendingActions[0]).toMatchObject({ status: "confirmed", backgroundJobId: "job_1" });
    expect(store.pendingActionLoadingIds).toEqual([]);
  });

  it("reconciles streamed content, references, and tool calls with persisted history", async () => {
    const streamed: SseEvent[] = [
      {
        id: "event_1",
        type: "tool.call",
        channel: "chat",
        timestamp: "2026-07-10T00:00:01.000Z",
        toolCall: {
          id: "tool_1",
          name: "knowledge_retrieval",
          status: "started",
          input: { query: "restart api" }
        }
      },
      {
        id: "event_2",
        type: "content.delta",
        channel: "chat",
        timestamp: "2026-07-10T00:00:01.000Z",
        delta: "Use the ",
        sequence: 1
      },
      {
        id: "event_3",
        type: "reference.source",
        channel: "chat",
        timestamp: "2026-07-10T00:00:02.000Z",
        reference: {
          id: "reference_1",
          title: "Restart runbook",
          sourceType: "document",
          documentId: "doc_1",
          chunkId: "chunk_1",
          score: 0.94
        }
      },
      {
        id: "event_4",
        type: "tool.call",
        channel: "chat",
        timestamp: "2026-07-10T00:00:02.000Z",
        toolCall: {
          id: "tool_1",
          name: "knowledge_retrieval",
          status: "completed",
          output: { results: 1 }
        }
      },
      {
        id: "event_5",
        type: "diagnostic.result",
        channel: "chat",
        timestamp: "2026-07-10T00:00:02.500Z",
        diagnostic: {
          taskId: "diagnostic_1",
          reportId: "report_1",
          rootCause: { primaryCause: "database_lock" },
          recoveryMode: "manual_review",
          executionPermitted: false,
          humanApprovalRequired: true,
          validatorStatus: "deterministic_grounded_fallback",
          evidenceIds: ["evidence_1"]
        }
      },
      {
        id: "event_6",
        type: "complete",
        channel: "chat",
        timestamp: "2026-07-10T00:00:03.000Z",
        result: {
          session: session(),
          message: message({
            metadata: {
              citations: [
                {
                  id: "reference_1",
                  title: "Restart runbook",
                  sourceType: "document",
                  documentId: "doc_1",
                  chunkId: "chunk_1",
                  score: 0.94
                }
              ],
              toolCallIds: ["tool_1"]
            }
          })
        }
      }
    ];
    setChatClientFactoryForTests(() => fakeClient({ streamed }));
    setActivePinia(createPinia());
    const store = useChatStore();

    await store.initialize();
    await store.selectSession("chat_1");
    await store.send("How do I restart the API?");

    expect(store.isSending).toBe(false);
    expect(store.messages).toEqual([
      message({
        metadata: {
          citations: [
            {
              id: "reference_1",
              title: "Restart runbook",
              sourceType: "document",
              documentId: "doc_1",
              chunkId: "chunk_1",
              score: 0.94
            }
          ],
          toolCallIds: ["tool_1"]
        }
      })
    ]);
    expect(store.references).toEqual([
      expect.objectContaining({ id: "reference_1", title: "Restart runbook" })
    ]);
    expect(store.toolAudits).toEqual([audit()]);
    expect(store.liveToolCalls).toEqual([]);
    expect(store.diagnosticResults[0]).toMatchObject({
      taskId: "diagnostic_1",
      executionPermitted: false,
      recoveryMode: "manual_review"
    });
  });

  it("sorts live references by rerank score and keeps only five", async () => {
    const references: SseEvent[] = [0.42, 0.98, 0.61, 0.75, 0.88, 0.53].map(
      (rerankScore, index) => ({
        id: `event_ref_${index}`,
        type: "reference.source",
        channel: "chat",
        timestamp: "2026-07-10T00:00:02.000Z",
        reference: {
          id: `reference_${index}`,
          title: `Source ${index}`,
          sourceType: "knowledge-base",
          score: rerankScore,
          vectorScore: 0.9 - index / 100,
          rerankScore
        }
      })
    );
    references.push({
      id: "event_complete",
      type: "complete",
      channel: "chat",
      timestamp: "2026-07-10T00:00:03.000Z",
      result: {
        session: session(),
        message: message({
          metadata: {
            citations: references.flatMap((event) =>
              event.type === "reference.source" ? [event.reference] : []
            )
          }
        })
      }
    });
    setChatClientFactoryForTests(() => fakeClient({ streamed: references }));
    setActivePinia(createPinia());
    const store = useChatStore();

    await store.initialize();
    await store.selectSession("chat_1");
    await store.send("rank sources");

    expect(store.references.map((reference) => reference.rerankScore)).toEqual([
      0.98,
      0.88,
      0.75,
      0.61,
      0.53
    ]);
  });

  it("clears the previous turn references as soon as a new turn starts", async () => {
    let releaseStream: (() => void) | undefined;
    const streamGate = new Promise<void>((resolve) => {
      releaseStream = resolve;
    });
    const previousAnswer = message({
      metadata: {
        citations: [
          {
            id: "old_reference",
            title: "Old runbook",
            sourceType: "knowledge-base",
            score: 0.91
          }
        ]
      }
    });
    const client = fakeClient({ historyMessage: previousAnswer });
    let latestTurnCompleted = false;
    client.getSession = async () => ({
      session: session(),
      messages: [latestTurnCompleted ? message({ metadata: {} }) : previousAnswer]
    });
    client.streamMessage = async function* (): AsyncIterable<SseEvent> {
      await streamGate;
      latestTurnCompleted = true;
      yield {
        id: "event_complete_new_turn",
        type: "complete",
        channel: "chat",
        timestamp: "2026-07-10T00:00:03.000Z",
        result: { session: session(), message: message({ metadata: {} }) }
      };
    };
    setChatClientFactoryForTests(() => client);
    setActivePinia(createPinia());
    const store = useChatStore();
    await store.initialize();
    expect(store.references.map((reference) => reference.id)).toEqual(["old_reference"]);

    const sending = store.send("new question");
    await Promise.resolve();

    expect(store.references).toEqual([]);
    releaseStream?.();
    await sending;
    expect(store.references).toEqual([]);
  });

  it("appends one server content chunk without typewriter timers", async () => {
    const timeout = vi.spyOn(globalThis, "setTimeout");
    const streamed: SseEvent[] = [
      {
        id: "event_content_typewriter",
        type: "content.delta",
        channel: "chat",
        timestamp: "2026-07-10T00:00:01.000Z",
        delta: "ABC",
        sequence: 1
      },
      {
        id: "event_complete_typewriter",
        type: "complete",
        channel: "chat",
        timestamp: "2026-07-10T00:00:02.000Z",
        result: {
          session: session(),
          message: message({ content: "ABC" })
        }
      }
    ];
    setChatClientFactoryForTests(() => fakeClient({ streamed }));
    setActivePinia(createPinia());
    const store = useChatStore();
    await store.initialize();

    await store.send("render one chunk");

    expect(store.messages[0]?.content).toBe("ABC");
    expect(timeout).not.toHaveBeenCalled();
  });

  it("clears visible conversation state on logout without deleting server sessions", async () => {
    setChatClientFactoryForTests(() => fakeClient());
    setActivePinia(createPinia());
    const store = useChatStore();
    await store.initialize();
    await store.selectSession("chat_1");

    store.reset();

    expect(store.sessions).toEqual([]);
    expect(store.messages).toEqual([]);
    expect(store.activeSessionId).toBeNull();
  });

  it("updates and compacts memory for only the active session", async () => {
    setChatClientFactoryForTests(() => fakeClient());
    setActivePinia(createPinia());
    const store = useChatStore();
    await store.initialize();

    await store.updateMemoryMode("manual");
    expect(store.activeSession?.memory.mode).toBe("manual");

    await store.compactMemory();
    expect(store.activeSession?.memory.contextUsagePercent).toBe(0.2);
    expect(store.isUpdatingMemory).toBe(false);
  });

  it("uses durable runs and clears tentative content after a worker restart", async () => {
    const client = fakeClient({ historyMessage: message({ content: "new answer" }) });
    const run: ChatRun = {
      id: "run_1",
      sessionId: "chat_1",
      clientRequestId: "request_1",
      status: "queued",
      lastEventSequence: 0,
      errorCode: null,
      createdAt: "2026-08-22T00:00:00Z",
      updatedAt: "2026-08-22T00:00:00Z"
    };
    client.createRun = async () => run;
    client.getRun = async () => ({ ...run, status: "succeeded", lastEventSequence: 4 });
    client.getActiveRun = async () => null;
    client.streamRunEvents = async function* (): AsyncIterable<SseEvent> {
      yield { id: "1", type: "content.delta", channel: "chat", timestamp: run.createdAt, delta: "old", sequence: 1 };
      yield { id: "2", type: "run.restarted", channel: "chat", timestamp: run.createdAt, runId: run.id, attempt: 2 };
      yield { id: "3", type: "content.delta", channel: "chat", timestamp: run.createdAt, delta: "new", sequence: 2 };
      yield { id: "4", type: "complete", channel: "chat", timestamp: run.createdAt };
    };
    setChatClientFactoryForTests(() => client);
    setActivePinia(createPinia());
    const store = useChatStore();
    await store.initialize();

    await store.send("diagnose");

    expect(store.messages.at(-1)?.content).toBe("new answer");
    expect(store.messages.some((item) => item.content.includes("old"))).toBe(false);
    expect(store.activeRunId).toBeNull();
  });
});

function fakeClient(
  options: {
    readonly historyMessage?: ChatMessage;
    readonly streamed?: readonly SseEvent[];
  } = {}
): ChatClient {
  let persistedMessage = options.historyMessage ?? message();
  return {
    createSession: async () => session({ id: "chat_new", title: "New chat" }),
    deleteSession: async (sessionId) => ({ sessionId, deleted: true }),
    getSession: async () => ({
      session: session(),
      messages: [persistedMessage]
    }),
    listSessions: async () => ({ items: [session()] }),
    listToolCallAudits: async () => ({ items: [audit()] }),
    updateMemoryMode: async (_sessionId, mode) => session({ memory: { ...session().memory, mode } }),
    compactMemory: async () => session({ memory: { ...session().memory, contextUsagePercent: 0.2 } }),
    streamMessage: async function* (): AsyncIterable<SseEvent> {
      for (const event of options.streamed ?? []) {
        if (event.type === "complete" && event.channel === "chat") {
          const result = event.result as { readonly message?: ChatMessage };
          if (result.message !== undefined) persistedMessage = result.message;
        }
        yield event;
      }
    }
  };
}
