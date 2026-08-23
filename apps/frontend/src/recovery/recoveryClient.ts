import type {
  RecoveryEventListResponse,
  RecoveryIntentResponse
} from "@agent-py/api-contracts";

import { createApiClient } from "../api/apiClient";
import { AUTH_TOKEN_STORAGE_KEY } from "../authClient";
import { API_BASE_URL } from "../config";

export interface RecoveryClient {
  createIntent(diagnosticTaskId: string, note?: string): Promise<RecoveryIntentResponse>;
  getIntent(intentId: string): Promise<RecoveryIntentResponse>;
  listEvents(intentId: string, afterSequence: number): Promise<RecoveryEventListResponse>;
  approveIntent(intentId: string, incidentIdConfirmation: string): Promise<RecoveryIntentResponse>;
  rejectIntent(intentId: string): Promise<RecoveryIntentResponse>;
  cancelIntent(intentId: string): Promise<RecoveryIntentResponse>;
}

export interface CreateRecoveryClientOptions {
  readonly baseUrl?: string;
  readonly fetchImpl?: typeof fetch;
  readonly getAccessToken?: () => string | null;
}

export function createRecoveryClient(options: CreateRecoveryClientOptions = {}): RecoveryClient {
  const api = createApiClient({
    baseUrl: options.baseUrl ?? API_BASE_URL,
    ...(options.fetchImpl === undefined ? {} : { fetchImpl: options.fetchImpl }),
    getAccessToken: options.getAccessToken ??
      (() => window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY))
  });
  const intentPath = (intentId: string): string =>
    `/aiops/recovery-intents/${encodeURIComponent(intentId)}`;

  return {
    createIntent: (diagnosticTaskId, note) =>
      api.request<RecoveryIntentResponse>(
        `/aiops/diagnostics/${encodeURIComponent(diagnosticTaskId)}/recovery-intents`,
        { method: "POST", body: JSON.stringify(note === undefined ? {} : { note }) }
      ),
    getIntent: (intentId) => api.request<RecoveryIntentResponse>(intentPath(intentId)),
    listEvents: (intentId, afterSequence) =>
      api.request<RecoveryEventListResponse>(
        `${intentPath(intentId)}/events?afterSequence=${encodeURIComponent(String(afterSequence))}`
      ),
    approveIntent: (intentId, incidentIdConfirmation) =>
      api.request<RecoveryIntentResponse>(`${intentPath(intentId)}:approve`, {
        method: "POST",
        body: JSON.stringify({ incidentIdConfirmation })
      }),
    rejectIntent: (intentId) =>
      api.request<RecoveryIntentResponse>(`${intentPath(intentId)}:reject`, { method: "POST" }),
    cancelIntent: (intentId) =>
      api.request<RecoveryIntentResponse>(`${intentPath(intentId)}:cancel`, { method: "POST" })
  };
}
