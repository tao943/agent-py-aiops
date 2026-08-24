import {
  FOUNDATION_HEALTH_CONTRACT,
  SSE_EVENT_TYPES,
  buildSuccessResponse,
  type ApiSuccessResponse,
  type HealthContract,
  type SseEventType
} from "@agent-py/api-contracts";

export const WORKBENCH_NAVIGATION_LABELS = [
  "事件中心",
  "调查工作台",
  "运维助手",
  "知识中心",
  "Agent 配置",
  "集成中心",
  "系统状态"
] as const;

export function getFrontendFoundationHealth(): HealthContract {
  return FOUNDATION_HEALTH_CONTRACT;
}

export function buildHealthMessage(contract: HealthContract): string {
  return `${contract.service} ${contract.version} is ${contract.status}`;
}

export function describeSharedContractUsage(): ApiSuccessResponse<HealthContract> {
  return buildSuccessResponse(FOUNDATION_HEALTH_CONTRACT, {
    requestId: "frontend-foundation"
  });
}

export function getRequiredSseEventTypes(): readonly SseEventType[] {
  return SSE_EVENT_TYPES;
}
