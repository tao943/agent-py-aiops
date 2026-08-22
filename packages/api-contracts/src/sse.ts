import type { ApiErrorMessage } from "./responses";

export const SSE_EVENT_TYPES = [
  "content.delta",
  "tool.call",
  "reference.source",
  "diagnostic.result",
  "run.status",
  "run.restarted",
  "execution.mode_selected",
  "structured.result",
  "confirmation.required",
  "confirmation.resolved",
  "explanation.delta",
  "explanation.degraded",
  "budget.exhausted",
  "task.status",
  "report",
  "complete",
  "error"
] as const;

export type SseEventType = (typeof SSE_EVENT_TYPES)[number];
export type SseChannel = "chat" | "aiops";

export interface SseEventBase<TType extends SseEventType> {
  readonly id: string;
  readonly type: TType;
  readonly channel: SseChannel;
  readonly timestamp: string;
}

export interface ContentDeltaSseEvent extends SseEventBase<"content.delta"> {
  readonly delta: string;
  readonly sequence: number;
}

export interface ToolCallSseEvent extends SseEventBase<"tool.call"> {
  readonly toolCall: {
    readonly id: string;
    readonly name: string;
    readonly status: "started" | "delta" | "completed" | "failed";
    readonly input?: unknown;
    readonly output?: unknown;
  };
}

export interface DiagnosticResultSseEvent extends SseEventBase<"diagnostic.result"> {
  readonly diagnostic: {
    readonly taskId: string;
    readonly reportId: string;
    readonly rootCause: Record<string, unknown>;
    readonly recoveryMode: string;
    readonly executionPermitted: boolean;
    readonly humanApprovalRequired: boolean;
    readonly validatorStatus: string;
    readonly evidenceIds: readonly string[];
  };
}

export interface RunStatusSseEvent extends SseEventBase<"run.status"> {
  readonly run: {
    readonly id: string;
    readonly status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  };
}

export interface RunRestartedSseEvent extends SseEventBase<"run.restarted"> {
  readonly runId: string;
  readonly attempt: number;
}

export interface ExecutionModeSelectedSseEvent
  extends SseEventBase<"execution.mode_selected"> {
  readonly mode: "direct_read" | "confirmation_required" | "bounded_react";
  readonly requiredCapability: string;
  readonly postcondition: string;
}

export interface StructuredResultSseEvent extends SseEventBase<"structured.result"> {
  readonly [key: string]: unknown;
}

export interface ConfirmationRequiredSseEvent
  extends SseEventBase<"confirmation.required"> {
  readonly action: import("./chat").PendingChatAction;
}

export interface ConfirmationResolvedSseEvent
  extends SseEventBase<"confirmation.resolved"> {
  readonly action: import("./chat").PendingChatAction;
}

export interface ExplanationDeltaSseEvent extends SseEventBase<"explanation.delta"> {
  readonly delta: string;
}

export interface ExplanationDegradedSseEvent
  extends SseEventBase<"explanation.degraded"> {
  readonly code: "CHAT_EXPLANATION_DEGRADED";
  readonly retryable: boolean;
}

export interface BudgetExhaustedSseEvent extends SseEventBase<"budget.exhausted"> {
  readonly code: "CHAT_EXECUTION_BUDGET_EXHAUSTED";
}

export interface ReferenceSourceSseEvent extends SseEventBase<"reference.source"> {
  readonly reference: {
    readonly id: string;
    readonly title: string;
    readonly sourceType: "knowledge-base" | "log" | "document" | "url";
    readonly chunkId?: string;
    readonly documentId?: string;
    readonly knowledgeBaseId?: string;
    readonly source?: string;
    readonly uri?: string;
    readonly metadata?: Record<string, unknown>;
    readonly score?: number;
    readonly vectorRank?: number;
    readonly bm25Rank?: number;
    readonly rerankRank?: number;
    readonly vectorScore?: number;
    readonly bm25Score?: number;
    readonly rrfScore?: number;
    readonly rerankScore?: number;
    readonly excerpt?: string;
    readonly knowledgeType?: "document" | "sop" | "diagnostic-case";
  };
}

export interface TaskStatusSseEvent extends SseEventBase<"task.status"> {
  readonly task: {
    readonly id: string;
    readonly status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
    readonly progress?: number;
    readonly message?: string;
  };
}

export interface ReportSseEvent extends SseEventBase<"report"> {
  readonly report: {
    readonly id: string;
    readonly title: string;
    readonly content: string;
    readonly format: "markdown" | "json";
  };
}

export interface CompleteSseEvent extends SseEventBase<"complete"> {
  readonly result?: unknown;
}

export interface ErrorSseEvent extends SseEventBase<"error"> {
  readonly error: ApiErrorMessage;
}

export type SseEvent =
  | ContentDeltaSseEvent
  | ToolCallSseEvent
  | ReferenceSourceSseEvent
  | DiagnosticResultSseEvent
  | RunStatusSseEvent
  | RunRestartedSseEvent
  | ExecutionModeSelectedSseEvent
  | StructuredResultSseEvent
  | ConfirmationRequiredSseEvent
  | ConfirmationResolvedSseEvent
  | ExplanationDeltaSseEvent
  | ExplanationDegradedSseEvent
  | BudgetExhaustedSseEvent
  | TaskStatusSseEvent
  | ReportSseEvent
  | CompleteSseEvent
  | ErrorSseEvent;
