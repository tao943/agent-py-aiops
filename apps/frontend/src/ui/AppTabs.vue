<script setup lang="ts">
interface TabItem {
  readonly id: string;
  readonly label: string;
  readonly disabled?: boolean;
}

defineProps<{
  readonly modelValue: string;
  readonly label: string;
  readonly items: readonly TabItem[];
}>();

const emit = defineEmits<{ "update:modelValue": [value: string] }>();
</script>

<template>
  <div class="app-tabs" role="tablist" :aria-label="label">
    <button
      v-for="item in items"
      :id="`tab-${item.id}`"
      :key="item.id"
      class="app-tabs__tab"
      type="button"
      role="tab"
      :aria-controls="`panel-${item.id}`"
      :aria-selected="modelValue === item.id ? 'true' : 'false'"
      :disabled="item.disabled"
      :tabindex="modelValue === item.id ? 0 : -1"
      @click="emit('update:modelValue', item.id)"
    >
      {{ item.label }}
    </button>
  </div>
</template>

<style scoped>
.app-tabs { align-items: center; border-bottom: 1px solid var(--line); display: flex; gap: 1rem; min-height: 2.8rem; }
.app-tabs__tab { align-self: stretch; color: var(--text-secondary); font-size: 0.82rem; padding: 0 0.15rem; position: relative; }
.app-tabs__tab[aria-selected="true"] { color: var(--text-primary); font-weight: 720; }
.app-tabs__tab[aria-selected="true"]::after { background: var(--accent); bottom: -1px; content: ""; height: 2px; left: 0; position: absolute; right: 0; }
</style>
