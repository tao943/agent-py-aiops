export type RuntimeReadinessStatus = "ready" | "degraded";

export type RuntimeDependencyName = "postgresql" | "milvus" | "llm" | "mcp" | "redis";

export type RuntimeDependencyStatus = "ready" | "unavailable";

export interface RuntimeDependencyReadiness {
  readonly name: RuntimeDependencyName;
  readonly status: RuntimeDependencyStatus;
  readonly safeSummary: string;
  readonly latencyMs?: number;
}

export interface RuntimeReadiness {
  readonly status: RuntimeReadinessStatus;
  readonly checkedAt: string;
  readonly dependencies: readonly RuntimeDependencyReadiness[];
}
