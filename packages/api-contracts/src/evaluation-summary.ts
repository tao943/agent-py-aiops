export interface EvaluationRunSummary {
  readonly runId: string;
  readonly evaluationKind: string;
  readonly scenarioId: string;
  readonly mode: string;
  readonly status: string;
  readonly total: number | null;
  readonly passed: boolean | null;
  readonly completedAt: string | null;
}

export interface EvaluationRunSummaryListResponse {
  readonly items: readonly EvaluationRunSummary[];
}
