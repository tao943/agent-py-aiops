<script setup lang="ts">
withDefaults(defineProps<{
  readonly variant?: "primary" | "secondary" | "quiet" | "danger";
  readonly size?: "small" | "medium";
  readonly type?: "button" | "submit" | "reset";
  readonly disabled?: boolean;
  readonly loading?: boolean;
}>(), {
  variant: "secondary",
  size: "medium",
  type: "button",
  disabled: false,
  loading: false
});
</script>

<template>
  <button
    class="app-button"
    :class="[`app-button--${variant}`, `app-button--${size}`]"
    :type="type"
    :disabled="disabled || loading"
    :aria-busy="loading ? 'true' : undefined"
  >
    <span v-if="loading" class="app-button__spinner" aria-hidden="true" />
    <slot />
  </button>
</template>

<style scoped>
.app-button { align-items: center; border: 1px solid transparent; border-radius: var(--radius-control); display: inline-flex; font-weight: 680; gap: 0.45rem; justify-content: center; min-height: 2.75rem; padding: 0 0.9rem; transition: background var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast); }
.app-button--small { min-height: 2.25rem; padding-inline: 0.7rem; }
.app-button--primary { background: var(--accent); color: #fff; }
.app-button--primary:hover:not(:disabled) { background: var(--accent-strong); }
.app-button--secondary { background: var(--surface-raised); border-color: var(--line-strong); }
.app-button--secondary:hover:not(:disabled), .app-button--quiet:hover:not(:disabled) { background: var(--surface-hover); }
.app-button--quiet { color: var(--text-secondary); }
.app-button--danger { background: var(--danger); color: #fff; }
.app-button:disabled { opacity: 0.58; }
.app-button__spinner { animation: app-button-spin 800ms linear infinite; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; height: 0.9rem; width: 0.9rem; }
@keyframes app-button-spin { to { transform: rotate(1turn); } }
</style>
