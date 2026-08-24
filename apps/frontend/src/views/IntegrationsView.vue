<script setup lang="ts">
import { Bell, Bot, Database, RadioTower, ScrollText } from "lucide-vue-next";
import McpView from "./McpView.vue";
const groups = [
  { label: "告警与指标", detail: "Alertmanager Webhook", icon: RadioTower },
  { label: "日志与检索", detail: "CLS / MCP 工具", icon: ScrollText },
  { label: "模型服务", detail: "LLM / Embedding / Rerank", icon: Bot },
  { label: "通知出口", detail: "待接入通知通道", icon: Bell },
  { label: "数据基础设施", detail: "PostgreSQL / Milvus / Redis", icon: Database }
] as const;
</script>
<template>
  <section class="integrations" aria-label="集成中心">
    <header><div><span>INTEGRATION CONTROL</span><h1>集成中心</h1><p>管理外部数据源与工具连接；运行健康和配置有效性统一在系统状态中查看。</p></div><RouterLink to="/system">查看运行状态</RouterLink></header>
    <div class="integrations__groups" aria-label="集成能力分组"><article v-for="group in groups" :key="group.label"><component :is="group.icon" :size="17" /><span><strong>{{ group.label }}</strong><small>{{ group.detail }}</small></span></article></div>
    <McpView :embedded="true" />
  </section>
</template>
<style scoped>
.integrations { background: var(--surface-raised); display: grid; grid-template-rows: auto auto minmax(0,1fr); height: 100%; min-height: 0; }
.integrations > header { align-items: center; background: var(--surface-panel); border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; padding: 1rem 1.5rem; }.integrations > header span { color: var(--accent); font-size: 0.62rem; font-weight: 800; letter-spacing: 0.1em; }.integrations h1 { font-size: 1.08rem; margin: 0.15rem 0; }.integrations > header p { color: var(--text-secondary); font-size: 0.72rem; margin: 0; }.integrations > header a { border: 1px solid var(--line-strong); border-radius: var(--radius-control); font-size: 0.7rem; font-weight: 700; padding: 0.7rem; text-decoration: none; }
.integrations__groups { border-bottom: 1px solid var(--line); display: grid; grid-template-columns: repeat(5, minmax(0,1fr)); }.integrations__groups article { align-items: center; display: grid; gap: 0.55rem; grid-template-columns: auto minmax(0,1fr); min-height: 4.4rem; padding: 0.8rem 1rem; }.integrations__groups article + article { border-left: 1px solid var(--line); }.integrations__groups svg { color: var(--accent); }.integrations__groups span { display: grid; gap: 0.2rem; }.integrations__groups strong { font-size: 0.7rem; }.integrations__groups small { color: var(--text-tertiary); font-size: 0.62rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 1000px) { .integrations { height: auto; }.integrations__groups { grid-template-columns: repeat(2, minmax(0,1fr)); }.integrations__groups article { border-bottom: 1px solid var(--line); }.integrations__groups article + article { border-left: 0; } }
@media (max-width: 600px) { .integrations > header { align-items: flex-start; flex-direction: column; gap: 0.8rem; }.integrations__groups { grid-template-columns: 1fr; } }
</style>
