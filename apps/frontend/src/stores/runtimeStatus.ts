import { computed, ref } from "vue";
import { defineStore } from "pinia";
import { createRuntimeStatusClient, type RuntimeStatusClient, type RuntimeStatusSnapshot } from "../runtime/runtimeStatusClient";
import { toUserFacingError } from "../ui/userFacingError";

let clientFactory: () => RuntimeStatusClient = createRuntimeStatusClient;
export function setRuntimeStatusClientFactoryForTests(factory: (() => RuntimeStatusClient) | null): void { clientFactory = factory ?? createRuntimeStatusClient; }

export const useRuntimeStatusStore = defineStore("runtime-status", () => {
  const client = clientFactory();
  const snapshot = ref<RuntimeStatusSnapshot | null>(null);
  const loading = ref(false);
  const errorMessage = ref<string | null>(null);
  const lastSuccessfulAt = ref<string | null>(null);
  let timer: ReturnType<typeof setInterval> | null = null;
  const stale = computed(() => lastSuccessfulAt.value !== null && Date.now() - Date.parse(lastSuccessfulAt.value) > 90_000);
  async function refresh(): Promise<void> { loading.value = true; errorMessage.value = null; try { snapshot.value = await client.load(); lastSuccessfulAt.value = new Date().toISOString(); } catch (error) { errorMessage.value = toUserFacingError(error); throw error; } finally { loading.value = false; } }
  function startPolling(): void { if (timer !== null) return; timer = setInterval(() => { if (typeof document === "undefined" || document.visibilityState === "visible") void refresh().catch(() => undefined); }, 30_000); }
  function stopPolling(): void { if (timer !== null) clearInterval(timer); timer = null; }
  return { snapshot, loading, errorMessage, lastSuccessfulAt, stale, refresh, startPolling, stopPolling };
});
