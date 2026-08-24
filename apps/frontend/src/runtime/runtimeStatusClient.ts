import type { BackgroundJob, BackgroundJobListResponse, EvaluationRunSummary, EvaluationRunSummaryListResponse } from "@agent-py/api-contracts";
import { createApiClient } from "../api/apiClient";
import { AUTH_TOKEN_STORAGE_KEY } from "../authClient";
import { API_BASE_URL } from "../config";
import type { RuntimeHealth } from "../runtimeHealth";

export interface SafeRuntimeDependency { readonly name: string; readonly status: "ready" | "unavailable"; readonly blocking: boolean; readonly latencyMs: number | null; readonly safeSummary: string; }
export interface SafeRuntimeConfiguration { readonly name: string; readonly valid: boolean; readonly safeSummary: string; }
export interface RuntimeStatusSnapshot { readonly process: RuntimeHealth; readonly readinessStatus: "ready" | "degraded"; readonly checkedAt: string; readonly dependencies: readonly SafeRuntimeDependency[]; readonly configuration: readonly SafeRuntimeConfiguration[]; readonly jobs: readonly BackgroundJob[]; readonly evaluations: readonly EvaluationRunSummary[]; }
export interface RuntimeStatusClient { load(): Promise<RuntimeStatusSnapshot>; }
export interface CreateRuntimeStatusClientOptions { readonly baseUrl?: string; readonly fetchImpl?: typeof fetch; readonly getAccessToken?: () => string | null; }

interface RawComponent { readonly ok?: boolean; readonly valid?: boolean; readonly latencyMs?: number; }
interface RawReady { readonly status: "ready" | "degraded"; readonly dependencies: Readonly<Record<string, RawComponent>>; }
interface RawConfigurationCheck { readonly configuration: Readonly<Record<string, RawComponent>>; }

export function createRuntimeStatusClient(options: CreateRuntimeStatusClientOptions = {}): RuntimeStatusClient {
  const api = createApiClient({ baseUrl: options.baseUrl ?? API_BASE_URL, ...(options.fetchImpl === undefined ? {} : { fetchImpl: options.fetchImpl }), getAccessToken: options.getAccessToken ?? (() => window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY)) });
  return { async load() {
    const [process, ready, configuration, jobs, evaluations] = await Promise.all([
      api.request<RuntimeHealth>("/health"), api.request<RawReady>("/ready"), api.request<RawConfigurationCheck>("/config/check"),
      api.request<BackgroundJobListResponse>("/background-jobs"), api.request<EvaluationRunSummaryListResponse>("/evaluation/runs?limit=20")
    ]);
    return {
      process, readinessStatus: ready.status, checkedAt: new Date().toISOString(), jobs: jobs.items, evaluations: evaluations.items,
      dependencies: Object.entries(ready.dependencies).map(([name, item]) => ({ name, status: item.ok === true ? "ready" : "unavailable", blocking: name !== "redis", latencyMs: typeof item.latencyMs === "number" ? item.latencyMs : null, safeSummary: item.ok === true ? "可用" : name === "redis" ? "不可用，缓存和分布式加速降级" : "不可用" })),
      configuration: Object.entries(configuration.configuration).map(([name, item]) => ({ name, valid: item.valid === true, safeSummary: item.valid === true ? "配置有效" : "配置无效" }))
    };
  } };
}
