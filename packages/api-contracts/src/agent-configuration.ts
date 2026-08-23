export type AgentResourceKind = "prompt" | "skill";

export type AgentVersionStatus = "draft" | "published" | "deprecated";

export type AgentNode =
  | "conversation"
  | "planner"
  | "replanner"
  | "investigator_runtime"
  | "investigator_log"
  | "investigator_change"
  | "adjudicator"
  | "validator"
  | "recovery_planner"
  | "report";

export interface AgentResource {
  readonly id: string;
  readonly kind: AgentResourceKind;
  readonly name: string;
  readonly description: string | null;
  readonly createdAt: string;
  readonly updatedAt: string;
}

export interface AgentResourceVersion {
  readonly id: string;
  readonly resourceId: string;
  readonly version: number;
  readonly status: AgentVersionStatus;
  readonly content: string;
  readonly spec: Readonly<Record<string, unknown>>;
  readonly createdAt: string;
  readonly publishedAt: string | null;
}

export interface AgentBinding {
  readonly id: string;
  readonly node: AgentNode;
  readonly promptVersionId: string | null;
  readonly skillVersionIds: readonly string[];
  readonly updatedAt: string;
}

export interface AgentConfigurationCapabilities {
  readonly canManageConfiguration: boolean;
}

export interface AgentConfigurationLibraryResponse {
  readonly resources: readonly AgentResource[];
  readonly versions: readonly AgentResourceVersion[];
  readonly bindings: readonly AgentBinding[];
  readonly capabilities: AgentConfigurationCapabilities;
}

export interface CreateAgentResourceRequest {
  readonly kind: AgentResourceKind;
  readonly name: string;
  readonly description?: string;
  readonly content: string;
  readonly spec?: Readonly<Record<string, unknown>>;
}

export interface UpdateAgentDraftRequest {
  readonly name: string;
  readonly description?: string;
  readonly content: string;
  readonly spec: Readonly<Record<string, unknown>>;
}

export interface AgentResourceMutationResponse {
  readonly resource: AgentResource;
  readonly version: AgentResourceVersion;
  readonly capabilities: AgentConfigurationCapabilities;
}

export interface UpdateAgentBindingRequest {
  readonly promptVersionId: string | null;
  readonly skillVersionIds: readonly string[];
}

export interface AgentBindingMutationResponse {
  readonly binding: AgentBinding;
  readonly capabilities: AgentConfigurationCapabilities;
}

export type AgentConfigurationAuditAction =
  | "resource_created"
  | "draft_saved"
  | "version_published"
  | "version_deprecated"
  | "binding_updated";

export interface AgentConfigurationAuditEvent {
  readonly id: string;
  readonly resourceId: string | null;
  readonly versionId: string | null;
  readonly bindingId: string | null;
  readonly action: AgentConfigurationAuditAction;
  readonly actorUserId: string;
  readonly safeSummary: string;
  readonly createdAt: string;
}

export interface AgentConfigurationAuditListResponse {
  readonly items: readonly AgentConfigurationAuditEvent[];
  readonly nextCursor: string | null;
}

export interface AgentRuntimeConfigurationSnapshot {
  readonly id: string;
  readonly node: AgentNode;
  readonly promptVersionId: string | null;
  readonly skillVersionIds: readonly string[];
  readonly contentDigests: Readonly<Record<string, string>>;
  readonly effectiveTools: readonly string[];
  readonly policyGateRequired: true;
  readonly createdAt: string;
}
