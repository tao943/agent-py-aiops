export type RecoveryAction = "restart_compose_service" | "terminate_postgres_blocker";

export type RecoveryRiskTier = "low" | "high";

export type RecoveryStatus =
  | "proposed"
  | "awaiting_approval"
  | "queued"
  | "revalidating"
  | "executing"
  | "verifying"
  | "recovered"
  | "denied"
  | "rejected"
  | "expired"
  | "cancelled"
  | "verification_failed"
  | "manual_intervention";

export type RecoveryCheckStatus = "passed" | "failed" | "pending";

export interface RecoveryCheck {
  readonly key: string;
  readonly status: RecoveryCheckStatus;
  readonly safeSummary: string;
  readonly checkedAt: string | null;
}

export interface RecoveryIntent {
  readonly id: string;
  readonly incidentId: string;
  readonly diagnosticTaskId: string;
  readonly reportId: string;
  readonly action: RecoveryAction;
  readonly targetKey: string;
  readonly riskTier: RecoveryRiskTier;
  readonly automaticEligible: boolean;
  readonly approvalRequired: boolean;
  readonly status: RecoveryStatus;
  readonly proposalFingerprint: string;
  readonly createdAt: string;
  readonly approvalExpiresAt: string | null;
  readonly startedAt: string | null;
  readonly completedAt: string | null;
  readonly safeReasonCode: string | null;
  readonly executionSummary: string | null;
  readonly verification: readonly RecoveryCheck[];
}

export interface RecoveryAuditEvent {
  readonly sequence: number;
  readonly type: string;
  readonly fromStatus: RecoveryStatus | null;
  readonly toStatus: RecoveryStatus;
  readonly safeReasonCode: string | null;
  readonly safeSummary: string;
  readonly durationMs: number | null;
  readonly createdAt: string;
}

export interface CreateRecoveryIntentRequest {
  readonly note?: string;
}

export interface ApproveRecoveryIntentRequest {
  readonly incidentIdConfirmation: string;
}

export interface RecoveryIntentResponse {
  readonly intent: RecoveryIntent;
}

export interface RecoveryEventListResponse {
  readonly items: readonly RecoveryAuditEvent[];
}

