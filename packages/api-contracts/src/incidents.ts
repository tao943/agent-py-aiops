import type { AiopsDiagnosticEvidenceChain, AiopsDiagnosticStatus } from "./protected-data";
import type { RecoveryAuditEvent, RecoveryIntent, RecoveryStatus } from "./recovery";

export type IncidentStatus = "active" | "resolved";

export type IncidentSeverity = "critical" | "high" | "medium" | "low" | "info" | "unknown";

export type IncidentStage =
  | "alert"
  | "investigation"
  | "decision"
  | "recovery"
  | "verification"
  | "closed";

export type IncidentVerificationStatus = "pending" | "passed" | "failed" | "not_available";

export type IncidentAgentMode = "single" | "multi";

export type IncidentApprovalStatus = "pending" | "approved" | "rejected" | "expired";

export type IncidentRecoveryMode = "automatic" | "manual_review" | "not_available";

export type IncidentRecoveryExecutionStatus = RecoveryStatus | "not_available";

export interface IncidentSummary {
  readonly id: string;
  readonly status: IncidentStatus;
  readonly alertName: string;
  readonly service: string | null;
  readonly severity: IncidentSeverity;
  readonly firstSeenAt: string;
  readonly lastSeenAt: string;
  readonly updatedAt: string;
  readonly deliveryCount: number;
  readonly diagnosticTaskId: string | null;
  readonly diagnosticStatus: AiopsDiagnosticStatus | null;
  readonly verificationStatus: IncidentVerificationStatus;
  readonly currentStage: IncidentStage;
  readonly source: string | null;
  readonly environment: string | null;
  readonly assignee: string | null;
  readonly agentMode: IncidentAgentMode | null;
  readonly approvalStatus: IncidentApprovalStatus | null;
  readonly recoveryMode: IncidentRecoveryMode;
  readonly recoveryExecutionStatus: IncidentRecoveryExecutionStatus;
  readonly recoveryIntentId: string | null;
  readonly productionRecoveryExecution: boolean;
}

export interface IncidentDetail extends IncidentSummary {
  readonly summary: string | null;
  readonly alertLabels: Readonly<Record<string, string>>;
  readonly alertAnnotations: Readonly<Record<string, string>>;
  readonly evidenceChain: AiopsDiagnosticEvidenceChain | null;
  readonly recoveryIntent: RecoveryIntent | null;
  readonly recoveryEvents: readonly RecoveryAuditEvent[];
}

export interface IncidentListResponse {
  readonly items: readonly IncidentSummary[];
  readonly nextCursor: string | null;
}

export interface IncidentListQuery {
  readonly cursor?: string;
  readonly limit?: number;
  readonly status?: IncidentStatus;
  readonly severity?: IncidentSeverity;
  readonly service?: string;
}

export interface IncidentDetailResponse {
  readonly incident: IncidentDetail;
}
