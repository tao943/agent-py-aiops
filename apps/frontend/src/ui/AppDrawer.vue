<script setup lang="ts">
import { X } from "lucide-vue-next";
import { nextTick, ref, watch } from "vue";

const props = withDefaults(defineProps<{
  readonly open: boolean;
  readonly title: string;
  readonly returnFocusTo?: HTMLElement | null;
}>(), { returnFocusTo: null });

const emit = defineEmits<{ close: [] }>();
const closeButton = ref<HTMLButtonElement | null>(null);

watch(() => props.open, async (open, wasOpen) => {
  if (open) {
    await nextTick();
    closeButton.value?.focus();
  } else if (wasOpen) {
    await nextTick();
    props.returnFocusTo?.focus();
  }
});

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") emit("close");
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="app-drawer__backdrop" @click.self="emit('close')">
      <section class="app-drawer" role="dialog" aria-modal="true" :aria-label="title" @keydown="onKeydown">
        <header>
          <h2>{{ title }}</h2>
          <button ref="closeButton" type="button" aria-label="关闭" @click="emit('close')">
            <X :size="19" aria-hidden="true" />
          </button>
        </header>
        <div class="app-drawer__body"><slot /></div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.app-drawer__backdrop { background: rgb(15 22 20 / 42%); inset: 0; position: fixed; z-index: 50; }
.app-drawer { background: var(--surface-raised); box-shadow: var(--shadow-float); display: grid; grid-template-rows: auto minmax(0, 1fr); height: 100%; margin-left: auto; max-width: min(28rem, 92vw); width: 100%; }
.app-drawer header { align-items: center; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; min-height: 4rem; padding: 0 1.2rem; }
.app-drawer h2 { font-size: 1rem; margin: 0; }
.app-drawer header button { align-items: center; border-radius: var(--radius-control); display: inline-flex; height: 2.75rem; justify-content: center; width: 2.75rem; }
.app-drawer header button:hover { background: var(--surface-hover); }
.app-drawer__body { min-height: 0; overflow: auto; padding: 1.2rem; }
</style>
