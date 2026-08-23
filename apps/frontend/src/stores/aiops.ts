import { ref } from "vue";
import { defineStore } from "pinia";

import type {
  ActiveAlert,
  AiopsDiagnosticCase,
  AiopsDiagnosticEvidenceChain,
  AiopsDiagnosticSummary,
  CreateAiopsDiagnosticRequest,
  SseEvent
} from "@agent-py/api-contracts";

export type PublicSpecialistStatus = "supported" | "refuted" | "inconclusive" | "failed" | "timeout" | "unknown";

export interface PublicSpecialistResult {
  readonly role: string;
  readonly status: PublicSpecialistStatus;
  readonly safeSummary: string;
}

export interface PublicInvestigationResult {
  readonly agentMode: "single" | "multi" | "unknown";
  readonly specialists: readonly PublicSpecialistResult[];
  readonly validatorOrigin: string;
  readonly validatorMessage: string;
  readonly executionPermitted: boolean | null;
  readonly rootCauseSummary: string;
  readonly recoverySummary: string;
  readonly configurationVersionIds: readonly string[];
}

export function toPublicInvestigationResult(
  chain: AiopsDiagnosticEvidenceChain | null
): PublicInvestigationResult {
  const payload = chain?.task.resultPayload ?? {};
  const mode = text(payload.agentMode);
  const validation = record(payload.validation);
  const recoveryPolicy = record(payload.recoveryPolicy);
  const decision = record(payload.decision);
  const recovery = record(payload.recovery);
  const origin = text(validation.validationOrigin) ?? text(payload.validationOrigin) ?? "unknown";
  const specialists = array(payload.specialistResults).flatMap((item) => {
    const value = record(item);
    const role = text(value.role);
    if (role === null) return [];
    const status = specialistStatus(text(value.status));
    return [{
      role,
      status,
      safeSummary: text(value.safeSummary) ?? specialistFallback(status)
    }];
  });
  return {
    agentMode: mode === "single" || mode === "multi" ? mode : "unknown",
    specialists,
    validatorOrigin: origin,
    validatorMessage: validatorMessage(origin),
    executionPermitted: typeof recoveryPolicy.executionPermitted === "boolean"
      ? recoveryPolicy.executionPermitted
      : null,
    rootCauseSummary: text(decision.safeSummary) ?? text(payload.rootCauseSummary) ?? "尚未提供根因结论",
    recoverySummary: text(recovery.safeSummary) ?? text(payload.recoverySummary) ?? "尚未生成恢复提案",
    configurationVersionIds: array(payload.configurationVersionIds)
      .filter((item): item is string => typeof item === "string")
      .slice(0, 8)
  };
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function array(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

function specialistStatus(value: string | null): PublicSpecialistStatus {
  return value === "supported" || value === "refuted" || value === "inconclusive" ||
    value === "failed" || value === "timeout" ? value : "unknown";
}

function specialistFallback(status: PublicSpecialistStatus): string {
  if (status === "inconclusive") return "当前证据不足，未形成独立结论";
  if (status === "timeout") return "该调查分支未在时限内返回";
  if (status === "failed") return "该调查分支暂时不可用";
  return "未提供可公开的分支摘要";
}

function validatorMessage(origin: string): string {
  if (origin === "deterministic_grounded_fallback") {
    return "语义核验不可用，确定性证据通过，已转人工复核";
  }
  if (origin === "llm_confirmed" || origin === "llm_semantic") return "语义核验已完成";
  if (origin === "deterministic") return "确定性证据核验已完成";
  if (origin === "llm_failed") return "语义核验失败，禁止自动执行";
  return "核验状态未提供";
}

import { ApiClientError } from "../api/apiClient";
import { createAiopsClient, type AiopsClient } from "../aiops/aiopsClient";
import { useFeedbackStore } from "./feedback";
import { toUserFacingError } from "../ui/userFacingError";

let clientFactory: () => AiopsClient = createAiopsClient;

export function setAiopsClientFactoryForTests(factory: (() => AiopsClient) | null): void {
  clientFactory = factory ?? createAiopsClient;
}

export const useAiopsStore = defineStore("aiops", () => {
  const client = clientFactory();
  const history = ref<readonly AiopsDiagnosticSummary[]>([]);
  const activeAlerts = ref<readonly ActiveAlert[]>([]);
  const diagnosticCases = ref<readonly AiopsDiagnosticCase[]>([]);
  const alertsErrorMessage = ref<string | null>(null);
  const alertsLoading = ref(false);
  const activeDiagnosticId = ref<string | null>(null);
  const activeTask = ref<AiopsDiagnosticSummary | null>(null);
  const evidenceChain = ref<AiopsDiagnosticEvidenceChain | null>(null);
  const liveEvents = ref<readonly SseEvent[]>([]);
  const isLoading = ref(false);
  const isRunning = ref(false);
  const errorMessage = ref<string | null>(null);
  const savedCaseDocumentId = ref<string | null>(null);
  let resumedDiagnosticId: string | null = null;

  function reportError(error: unknown): void {
    const message = toUserFacingError(error);
    errorMessage.value = message;
    useFeedbackStore().showError(message);
  }

  function upsertHistory(task: AiopsDiagnosticSummary): void {
    history.value = [task, ...history.value.filter((item) => item.id !== task.id)]
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
  }

  async function reloadHistory(): Promise<void> {
    history.value = (await client.listDiagnostics()).items;
  }

  async function reloadActiveAlerts(): Promise<void> {
    alertsLoading.value = true;
    alertsErrorMessage.value = null;
    try {
      activeAlerts.value = (await client.listActiveAlerts()).items;
    } catch (error) {
      activeAlerts.value = [];
      alertsErrorMessage.value = "暂时无法获取活跃告警。";
    } finally {
      alertsLoading.value = false;
    }
  }

  async function reloadDiagnosticCases(): Promise<void> {
    diagnosticCases.value = (await client.listDiagnosticCases()).items;
  }

  async function loadEvidenceChain(diagnosticId: string): Promise<void> {
    const chain = await client.getEvidenceChain(diagnosticId);
    evidenceChain.value = chain;
    activeDiagnosticId.value = chain.task.id;
    activeTask.value = chain.task;
    upsertHistory(chain.task);
  }

  function reset(): void {
    history.value = [];
    activeAlerts.value = [];
    diagnosticCases.value = [];
    alertsErrorMessage.value = null;
    alertsLoading.value = false;
    activeDiagnosticId.value = null;
    activeTask.value = null;
    evidenceChain.value = null;
    liveEvents.value = [];
    isLoading.value = false;
    isRunning.value = false;
    errorMessage.value = null;
    savedCaseDocumentId.value = null;
  }

  async function runDiagnostic(
    query: string,
    alert?: Record<string, unknown>,
  ): Promise<void> {
    const request: CreateAiopsDiagnosticRequest = {
      query,
      ...(alert === undefined ? {} : { alert })
    };
    isRunning.value = true;
    errorMessage.value = null;
    liveEvents.value = [];
    evidenceChain.value = null;
    try {
      const created = await client.createDiagnostic(request);
      activeDiagnosticId.value = created.id;
      activeTask.value = created;
      upsertHistory(created);
      for await (const event of client.streamDiagnostic(created.id)) {
        liveEvents.value = [...liveEvents.value, event];
        if (event.type === "error") {
          reportError(new ApiClientError(event.error));
        }
      }
      await Promise.all([loadEvidenceChain(created.id), reloadHistory(), reloadDiagnosticCases()]);
    } catch (error) {
      reportError(error);
      try {
        if (activeDiagnosticId.value !== null) {
            await Promise.all([
              loadEvidenceChain(activeDiagnosticId.value),
              reloadHistory(),
              reloadDiagnosticCases()
            ]);
        }
      } catch (reconciliationError) {
        reportError(reconciliationError);
      }
    } finally {
      isRunning.value = false;
    }
  }

  async function diagnoseAlert(alert: ActiveAlert): Promise<void> {
    const query = `排查活跃告警：${alert.alertName}，服务：${alert.service}，级别：${alert.severity}。${alert.summary}`;
    await runDiagnostic(query, {
      ...alert.context,
      alertSource: alert.source,
      alertName: alert.alertName,
      service: alert.service,
      severity: alert.severity,
      status: alert.status,
      startsAt: alert.startsAt
    });
  }

  async function resumeDiagnostic(diagnosticId: string): Promise<void> {
    if (resumedDiagnosticId === diagnosticId) return;
    resumedDiagnosticId = diagnosticId;
    isRunning.value = true;
    errorMessage.value = null;
    liveEvents.value = [];
    try {
      await loadEvidenceChain(diagnosticId);
      if (activeTask.value?.status !== "accepted" && activeTask.value?.status !== "running") return;
      for await (const event of client.streamDiagnostic(diagnosticId)) {
        liveEvents.value = [...liveEvents.value, event];
        if (event.type === "error") reportError(new ApiClientError(event.error));
      }
      await Promise.all([
        loadEvidenceChain(diagnosticId),
        reloadHistory(),
        reloadDiagnosticCases().catch(() => undefined)
      ]);
    } catch (error) {
      reportError(error);
      throw error;
    } finally {
      isRunning.value = false;
      resumedDiagnosticId = null;
    }
  }

  return {
    activeDiagnosticId,
    activeAlerts,
    activeTask,
    diagnosticCases,
    alertsErrorMessage,
    alertsLoading,
    errorMessage,
    evidenceChain,
    history,
    isLoading,
    isRunning,
    liveEvents,
    savedCaseDocumentId,
    initialize: async (): Promise<void> => {
      isLoading.value = true;
      errorMessage.value = null;
      try {
        await reloadHistory();
      } catch (error) {
        reset();
        reportError(error);
        throw error;
      } finally {
        isLoading.value = false;
      }
      await Promise.all([reloadActiveAlerts(), reloadDiagnosticCases().catch(() => undefined)]);
    },
    selectDiagnostic: async (diagnosticId: string): Promise<void> => {
      isLoading.value = true;
      errorMessage.value = null;
      liveEvents.value = [];
      try {
        await loadEvidenceChain(diagnosticId);
      } catch (error) {
        evidenceChain.value = null;
        reportError(error);
        throw error;
      } finally {
        isLoading.value = false;
      }
    },
    diagnoseAlert,
    refreshActiveAlerts: reloadActiveAlerts,
    refreshDiagnosticCases: reloadDiagnosticCases,
    resumeDiagnostic,
    runDiagnostic,
    cancelActive: async (): Promise<void> => {
      const jobId = activeTask.value?.backgroundJob?.id;
      if (jobId === undefined || client.cancelBackgroundJob === undefined) return;
      try {
        await client.cancelBackgroundJob(jobId);
        isRunning.value = false;
        await Promise.all([reloadHistory(), loadEvidenceChain(activeTask.value!.id)]);
      } catch (error) {
        reportError(error);
        throw error;
      }
    },
    saveActiveCase: async (): Promise<void> => {
      if (activeDiagnosticId.value === null) return;
      try {
        const saved = await client.saveDiagnosticCase(activeDiagnosticId.value);
        savedCaseDocumentId.value = saved.document.id;
      } catch (error) {
        reportError(error);
        throw error;
      }
    },
    reset
  };
});
