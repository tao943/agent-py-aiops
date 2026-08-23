<script setup lang="ts">
import {
  Bot,
  BookOpen,
  Cable,
  HeartPulse,
  MessageSquare,
  SearchCheck,
  Siren
} from "lucide-vue-next";

const props = defineProps<{ readonly activePath: string }>();

const entries = [
  { icon: Siren, label: "事件中心", to: "/incidents", match: "center" },
  { icon: SearchCheck, label: "调查工作台", to: "/incidents", match: "investigation" },
  { icon: MessageSquare, label: "运维助手", to: "/assistant", match: "prefix" },
  { icon: BookOpen, label: "知识中心", to: "/knowledge", match: "prefix" },
  { icon: Bot, label: "Agent 配置", to: "/agent-config", match: "prefix" },
  { icon: Cable, label: "集成中心", to: "/integrations", match: "prefix" },
  { icon: HeartPulse, label: "系统状态", to: "/system", match: "prefix" }
] as const;

function isActive(entry: typeof entries[number]): boolean {
  if (entry.match === "center") return props.activePath === "/incidents";
  if (entry.match === "investigation") return props.activePath.startsWith("/incidents/");
  return props.activePath === entry.to || props.activePath.startsWith(`${entry.to}/`);
}
</script>

<template>
  <nav class="workspace-navigation" aria-label="工作区导航">
    <template v-for="entry in entries" :key="`${entry.label}-${entry.to}`">
      <RouterLink
        :to="entry.to"
        class="workspace-navigation__link"
        :class="{ 'workspace-navigation__link--active': isActive(entry) }"
        :aria-current="isActive(entry) ? 'page' : undefined"
      >
        <component :is="entry.icon" :size="18" stroke-width="1.8" aria-hidden="true" />
        <span>{{ entry.label }}</span>
      </RouterLink>
    </template>
  </nav>
</template>

<style scoped>
.workspace-navigation { display: grid; gap: 0.2rem; }
.workspace-navigation__link { align-items: center; border-radius: var(--radius-sm); color: var(--rail-muted); display: flex; font-size: 0.9rem; font-weight: 560; gap: 0.72rem; min-height: 2.7rem; padding: 0 0.7rem; text-decoration: none; transition: background var(--transition-fast), color var(--transition-fast); }
.workspace-navigation__link:hover { background: var(--rail-hover); color: var(--rail-text); }
.workspace-navigation__link--active { background: #343538; color: #ffffff; font-weight: 650; }
.workspace-navigation__link--active svg { color: #65d6b3; }
</style>
