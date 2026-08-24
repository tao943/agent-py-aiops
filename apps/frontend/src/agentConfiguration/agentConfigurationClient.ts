import type {
  AgentBindingMutationResponse,
  AgentConfigurationAuditListResponse,
  AgentConfigurationLibraryResponse,
  AgentNode,
  AgentResourceMutationResponse,
  CreateAgentResourceRequest,
  UpdateAgentBindingRequest,
  UpdateAgentDraftRequest
} from "@agent-py/api-contracts";

import { createApiClient } from "../api/apiClient";
import { AUTH_TOKEN_STORAGE_KEY } from "../authClient";
import { API_BASE_URL } from "../config";

export interface AgentVersionValidationResponse {
  readonly valid: boolean;
  readonly warnings: readonly string[];
}

export interface CreateAgentDraftRequest {
  readonly content: string;
  readonly spec: Readonly<Record<string, unknown>>;
  /** Client-only provenance used by the editor; the server derives the next version. */
  readonly sourceVersionId?: string;
}

export interface AgentConfigurationClient {
  listLibrary(): Promise<AgentConfigurationLibraryResponse>;
  listAudit(): Promise<AgentConfigurationAuditListResponse>;
  createResource(request: CreateAgentResourceRequest): Promise<AgentResourceMutationResponse>;
  createDraft(resourceId: string, request: CreateAgentDraftRequest): Promise<AgentResourceMutationResponse>;
  updateDraft(versionId: string, request: UpdateAgentDraftRequest): Promise<AgentResourceMutationResponse>;
  validateVersion(versionId: string): Promise<AgentVersionValidationResponse>;
  publishVersion(versionId: string): Promise<AgentResourceMutationResponse>;
  deprecateVersion(versionId: string): Promise<AgentResourceMutationResponse>;
  updateBinding(node: AgentNode, request: UpdateAgentBindingRequest): Promise<AgentBindingMutationResponse>;
}

export interface CreateAgentConfigurationClientOptions {
  readonly baseUrl?: string;
  readonly fetchImpl?: typeof fetch;
  readonly getAccessToken?: () => string | null;
}

export function createAgentConfigurationClient(
  options: CreateAgentConfigurationClientOptions = {}
): AgentConfigurationClient {
  const api = createApiClient({
    baseUrl: options.baseUrl ?? API_BASE_URL,
    ...(options.fetchImpl === undefined ? {} : { fetchImpl: options.fetchImpl }),
    getAccessToken: options.getAccessToken ??
      (() => window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY))
  });
  return {
    listLibrary: () => api.request("/agent-configuration/resources"),
    listAudit: () => api.request("/agent-configuration/audit"),
    createResource: (body) => api.request("/agent-configuration/resources", {
      method: "POST", body: JSON.stringify(body)
    }),
    createDraft: (resourceId, body) => api.request(
      `/agent-configuration/resources/${encodeURIComponent(resourceId)}/versions`,
      { method: "POST", body: JSON.stringify({ content: body.content, spec: body.spec }) }
    ),
    updateDraft: (versionId, body) => api.request(
      `/agent-configuration/versions/${encodeURIComponent(versionId)}`,
      { method: "PUT", body: JSON.stringify(body) }
    ),
    validateVersion: (versionId) => api.request(
      `/agent-configuration/versions/${encodeURIComponent(versionId)}:validate`,
      { method: "POST" }
    ),
    publishVersion: (versionId) => api.request(
      `/agent-configuration/versions/${encodeURIComponent(versionId)}:publish`,
      { method: "POST" }
    ),
    deprecateVersion: (versionId) => api.request(
      `/agent-configuration/versions/${encodeURIComponent(versionId)}:deprecate`,
      { method: "POST" }
    ),
    updateBinding: (node, body) => api.request(
      `/agent-configuration/bindings/${encodeURIComponent(node)}`,
      { method: "PUT", body: JSON.stringify(body) }
    )
  };
}
