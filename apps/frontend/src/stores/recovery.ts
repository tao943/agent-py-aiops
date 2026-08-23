import { computed, ref } from "vue";
import { defineStore } from "pinia";

import type {
  RecoveryAuditEvent,
  RecoveryIntent,
  RecoveryStatus
} from "@agent-py/api-contracts";

import { createRecoveryClient, type RecoveryClient } from "../recovery/recoveryClient";
import { toUserFacingError } from "../ui/userFacingError";

const POLL_INTERVAL_MS = 2_000;
const POLLING_STATUSES = new Set<RecoveryStatus>([
  "queued", "revalidating", "executing", "verifying"
]);

let clientFactory: () => RecoveryClient = createRecoveryClient;

export function setRecoveryClientFactoryForTests(factory: (() => RecoveryClient) | null): void {
  clientFactory = factory ?? createRecoveryClient;
}

export const useRecoveryStore = defineStore("recovery", () => {
  const client = clientFactory();
  const intentId = ref<string | null>(null);
  const intent = ref<RecoveryIntent | null>(null);
  const events = ref<readonly RecoveryAuditEvent[]>([]);
  const isRefreshing = ref(false);
  const actionPending = ref<"approve" | "reject" | "cancel" | null>(null);
  const stale = ref(false);
  const errorMessage = ref<string | null>(null);
  let timer: ReturnType<typeof setInterval> | null = null;
  let visibilityBound = false;
  let refreshPromise: Promise<void> | null = null;
  let lifecycle = 0;

  const canPoll = computed(() =>
    intent.value !== null && POLLING_STATUSES.has(intent.value.status)
  );
  const lastSequence = computed(() =>
    events.value.reduce((highest, event) => Math.max(highest, event.sequence), 0)
  );

  function clearTimer(): void {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  function schedule(): void {
    clearTimer();
    if (!canPoll.value || document.visibilityState !== "visible") return;
    timer = setInterval(() => { void refresh(); }, POLL_INTERVAL_MS);
  }

  function mergeEvents(incoming: readonly RecoveryAuditEvent[]): void {
    const bySequence = new Map(events.value.map((event) => [event.sequence, event]));
    for (const event of incoming) bySequence.set(event.sequence, event);
    events.value = [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
  }

  async function performRefresh(): Promise<void> {
    if (intentId.value === null) return;
    const activeLifecycle = lifecycle;
    isRefreshing.value = true;
    try {
      const currentId = intentId.value;
      const [intentResponse, eventResponse] = await Promise.all([
        client.getIntent(currentId),
        client.listEvents(currentId, lastSequence.value)
      ]);
      if (intentId.value !== currentId || activeLifecycle !== lifecycle) return;
      intent.value = intentResponse.intent;
      mergeEvents(eventResponse.items);
      stale.value = false;
      errorMessage.value = null;
    } catch (error) {
      if (activeLifecycle !== lifecycle) return;
      stale.value = intent.value !== null;
      errorMessage.value = toUserFacingError(error);
    } finally {
      if (activeLifecycle === lifecycle) {
        isRefreshing.value = false;
        schedule();
      }
    }
  }

  function refresh(): Promise<void> {
    if (refreshPromise !== null) return refreshPromise;
    const tracked = performRefresh().finally(() => {
      if (refreshPromise === tracked) refreshPromise = null;
    });
    refreshPromise = tracked;
    return tracked;
  }

  function onVisibilityChange(): void {
    if (document.visibilityState === "hidden") {
      clearTimer();
      return;
    }
    if (canPoll.value) void refresh();
  }

  function bindVisibility(): void {
    if (visibilityBound) return;
    document.addEventListener("visibilitychange", onVisibilityChange);
    visibilityBound = true;
  }

  function stop(): void {
    lifecycle += 1;
    clearTimer();
    if (visibilityBound) {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      visibilityBound = false;
    }
    refreshPromise = null;
  }

  async function start(
    nextIntentId: string,
    projectedIntent: RecoveryIntent | null = null,
    projectedEvents: readonly RecoveryAuditEvent[] = []
  ): Promise<void> {
    stop();
    intentId.value = nextIntentId;
    intent.value = projectedIntent;
    events.value = [...projectedEvents].sort((left, right) => left.sequence - right.sequence);
    stale.value = false;
    errorMessage.value = null;
    bindVisibility();
    await refresh();
  }

  async function mutate(
    action: "approve" | "reject" | "cancel",
    operation: () => Promise<{ readonly intent: RecoveryIntent }>
  ): Promise<void> {
    actionPending.value = action;
    errorMessage.value = null;
    try {
      intent.value = (await operation()).intent;
      stale.value = false;
      await refresh();
    } catch (error) {
      errorMessage.value = toUserFacingError(error);
      throw error;
    } finally {
      actionPending.value = null;
      schedule();
    }
  }

  return {
    intentId, intent, events, isRefreshing, actionPending, stale, errorMessage,
    canPoll, lastSequence, start, refresh, stop,
    approve: (incidentIdConfirmation: string): Promise<void> => {
      if (intentId.value === null) return Promise.resolve();
      return mutate("approve", () => client.approveIntent(intentId.value!, incidentIdConfirmation));
    },
    reject: (): Promise<void> => {
      if (intentId.value === null) return Promise.resolve();
      return mutate("reject", () => client.rejectIntent(intentId.value!));
    },
    cancel: (): Promise<void> => {
      if (intentId.value === null) return Promise.resolve();
      return mutate("cancel", () => client.cancelIntent(intentId.value!));
    }
  };
});
