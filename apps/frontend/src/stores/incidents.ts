import { computed, ref } from "vue";
import { defineStore } from "pinia";

import type {
  DiagnoseIncidentResponse,
  IncidentDetail,
  IncidentSeverity,
  IncidentStatus,
  IncidentSummary
} from "@agent-py/api-contracts";

import { createIncidentClient, type IncidentClient } from "../incidents/incidentClient";
import { toUserFacingError } from "../ui/userFacingError";

type StatusFilter = IncidentStatus | "all";
type SeverityFilter = IncidentSeverity | "all";

let clientFactory: () => IncidentClient = createIncidentClient;

export function setIncidentClientFactoryForTests(factory: (() => IncidentClient) | null): void {
  clientFactory = factory ?? createIncidentClient;
}

export const useIncidentStore = defineStore("incidents", () => {
  const client = clientFactory();
  const items = ref<readonly IncidentSummary[]>([]);
  const selectedId = ref<string | null>(null);
  const statusFilter = ref<StatusFilter>("all");
  const severityFilter = ref<SeverityFilter>("all");
  const nextCursor = ref<string | null>(null);
  const isLoading = ref(false);
  const isLoadingMore = ref(false);
  const errorMessage = ref<string | null>(null);
  const lastSuccessfulAt = ref<string | null>(null);
  const stale = ref(false);
  const diagnosingIds = ref<readonly string[]>([]);
  const lastDiagnosis = ref<DiagnoseIncidentResponse | null>(null);
  const detail = ref<IncidentDetail | null>(null);
  const isDetailLoading = ref(false);
  const detailErrorMessage = ref<string | null>(null);

  const visibleIncidents = computed(() => items.value.filter((item) =>
    (statusFilter.value === "all" || item.status === statusFilter.value) &&
    (severityFilter.value === "all" || item.severity === severityFilter.value)
  ));
  const selectedIncident = computed(() =>
    items.value.find((item) => item.id === selectedId.value) ?? null
  );
  const metrics = computed(() => {
    const active = items.value.filter((item) => item.status === "active");
    const hasProductionRecovery = items.value.some((item) => item.productionRecoveryExecution);
    const activeRecoveryStatuses = new Set(["queued", "revalidating", "executing", "verifying"]);
    return {
      activeCount: active.length,
      criticalCount: active.filter((item) => item.severity === "critical").length,
      pendingApprovalCount: active.filter((item) => item.approvalStatus === "pending").length,
      automaticRecoveryCount: active.filter((item) =>
        item.productionRecoveryExecution &&
        item.recoveryMode === "automatic" &&
        activeRecoveryStatuses.has(item.recoveryExecutionStatus)
      ).length,
      hasProductionRecovery
    };
  });

  function ensureSelection(): void {
    if (visibleIncidents.value.some((item) => item.id === selectedId.value)) return;
    selectedId.value = visibleIncidents.value[0]?.id ?? null;
  }

  async function initialize(): Promise<void> {
    isLoading.value = true;
    errorMessage.value = null;
    try {
      const page = await client.listIncidents({ status: "all", limit: 50 });
      items.value = page.items;
      nextCursor.value = page.nextCursor;
      lastSuccessfulAt.value = new Date().toISOString();
      stale.value = false;
      ensureSelection();
    } catch (error) {
      errorMessage.value = toUserFacingError(error);
      stale.value = items.value.length > 0;
      throw error;
    } finally {
      isLoading.value = false;
    }
  }

  async function loadMore(): Promise<void> {
    if (nextCursor.value === null || isLoadingMore.value) return;
    isLoadingMore.value = true;
    try {
      const page = await client.listIncidents({
        status: "all",
        limit: 50,
        cursor: nextCursor.value
      });
      const known = new Set(items.value.map((item) => item.id));
      items.value = [...items.value, ...page.items.filter((item) => !known.has(item.id))];
      nextCursor.value = page.nextCursor;
      lastSuccessfulAt.value = new Date().toISOString();
      stale.value = false;
    } catch (error) {
      errorMessage.value = toUserFacingError(error);
      stale.value = true;
      throw error;
    } finally {
      isLoadingMore.value = false;
    }
  }

  async function startDiagnostic(incidentId: string): Promise<DiagnoseIncidentResponse> {
    if (!diagnosingIds.value.includes(incidentId)) {
      diagnosingIds.value = [...diagnosingIds.value, incidentId];
    }
    try {
      const result = await client.diagnoseIncident(incidentId);
      lastDiagnosis.value = result;
      items.value = items.value.map((item) => item.id === incidentId ? {
        ...item,
        diagnosticTaskId: result.diagnosticTaskId,
        diagnosticStatus: "accepted",
        currentStage: "investigation"
      } : item);
      return result;
    } catch (error) {
      errorMessage.value = toUserFacingError(error);
      throw error;
    } finally {
      diagnosingIds.value = diagnosingIds.value.filter((item) => item !== incidentId);
    }
  }

  async function loadDetail(incidentId: string): Promise<IncidentDetail> {
    isDetailLoading.value = true;
    detailErrorMessage.value = null;
    try {
      const response = await client.getIncident(incidentId);
      detail.value = response.incident;
      selectedId.value = response.incident.id;
      return response.incident;
    } catch (error) {
      detailErrorMessage.value = toUserFacingError(error);
      throw error;
    } finally {
      isDetailLoading.value = false;
    }
  }

  return {
    items,
    selectedId,
    statusFilter,
    severityFilter,
    nextCursor,
    isLoading,
    isLoadingMore,
    errorMessage,
    lastSuccessfulAt,
    stale,
    diagnosingIds,
    lastDiagnosis,
    detail,
    isDetailLoading,
    detailErrorMessage,
    visibleIncidents,
    selectedIncident,
    metrics,
    initialize,
    loadMore,
    select: (incidentId: string): void => { selectedId.value = incidentId; },
    setStatusFilter: (value: StatusFilter): void => {
      statusFilter.value = value;
      ensureSelection();
    },
    setSeverityFilter: (value: SeverityFilter): void => {
      severityFilter.value = value;
      ensureSelection();
    },
    startDiagnostic,
    loadDetail
  };
});
