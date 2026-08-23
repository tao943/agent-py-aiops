import type {
  DiagnoseIncidentRequest,
  DiagnoseIncidentResponse,
  IncidentDetailResponse,
  IncidentListQuery,
  IncidentListResponse
} from "@agent-py/api-contracts";

import { createApiClient } from "../api/apiClient";
import { AUTH_TOKEN_STORAGE_KEY } from "../authClient";
import { API_BASE_URL } from "../config";

export interface IncidentClient {
  listIncidents(query?: IncidentListQuery): Promise<IncidentListResponse>;
  getIncident(incidentId: string): Promise<IncidentDetailResponse>;
  diagnoseIncident(
    incidentId: string,
    request?: DiagnoseIncidentRequest
  ): Promise<DiagnoseIncidentResponse>;
}

export interface CreateIncidentClientOptions {
  readonly baseUrl?: string;
  readonly fetchImpl?: typeof fetch;
  readonly getAccessToken?: () => string | null;
}

export function createIncidentClient(options: CreateIncidentClientOptions = {}): IncidentClient {
  const api = createApiClient({
    baseUrl: options.baseUrl ?? API_BASE_URL,
    ...(options.fetchImpl === undefined ? {} : { fetchImpl: options.fetchImpl }),
    getAccessToken: options.getAccessToken ??
      (() => window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY))
  });

  return {
    listIncidents: (query = {}) => {
      const search = new URLSearchParams();
      if (query.status !== undefined) search.set("status", query.status);
      if (query.limit !== undefined) search.set("limit", String(query.limit));
      if (query.cursor !== undefined) search.set("cursor", query.cursor);
      if (query.severity !== undefined) search.set("severity", query.severity);
      if (query.service !== undefined) search.set("service", query.service);
      const suffix = search.size > 0 ? `?${search.toString()}` : "";
      return api.request<IncidentListResponse>(`/aiops/incidents${suffix}`);
    },
    getIncident: (incidentId) =>
      api.request<IncidentDetailResponse>(
        `/aiops/incidents/${encodeURIComponent(incidentId)}`
      ),
    diagnoseIncident: (incidentId, request = {}) =>
      api.request<DiagnoseIncidentResponse>(
        `/aiops/incidents/${encodeURIComponent(incidentId)}:diagnose`,
        { method: "POST", body: JSON.stringify(request) }
      )
  };
}
