import {
  createMemoryHistory,
  createRouter,
  createWebHistory,
  type Router
} from "vue-router";

import WorkspaceLayout from "../layouts/WorkspaceLayout.vue";
import AuthView from "../views/AuthView.vue";
import AgentConfigurationView from "../views/AgentConfigurationView.vue";
import ChatView from "../views/ChatView.vue";
import IncidentCenterView from "../views/IncidentCenterView.vue";
import IncidentWorkspaceView from "../views/IncidentWorkspaceView.vue";
import IntegrationsView from "../views/IntegrationsView.vue";
import KnowledgeView from "../views/KnowledgeView.vue";
import SystemStatusView from "../views/SystemStatusView.vue";

export interface AuthRouteAccess {
  initialize(): Promise<void>;
  isAuthenticated(): boolean;
}

export function createAppRouter(auth: AuthRouteAccess): Router {
  const router = createRouter({
    history: typeof window === "undefined" ? createMemoryHistory() : createWebHistory(),
    routes: [
      { path: "/", redirect: "/incidents" },
      { path: "/chat", redirect: "/assistant" },
      { path: "/aiops", redirect: "/incidents" },
      { path: "/mcp", redirect: "/integrations" },
      {
        path: "/login",
        name: "login",
        component: AuthView,
        props: { mode: "login" },
        meta: { publicOnly: true }
      },
      {
        path: "/register",
        name: "register",
        component: AuthView,
        props: { mode: "register" },
        meta: { publicOnly: true }
      },
      {
        path: "/",
        component: WorkspaceLayout,
        meta: { requiresAuth: true },
        children: [
          {
            path: "incidents",
            name: "incidents",
            component: IncidentCenterView,
            meta: { title: "事件中心" }
          },
          {
            path: "incidents/:incidentId",
            name: "incident-workspace",
            component: IncidentWorkspaceView,
            meta: { title: "调查工作台" }
          },
          {
            path: "assistant",
            name: "assistant",
            component: ChatView,
            meta: { title: "运维助手" }
          },
          {
            path: "knowledge",
            name: "knowledge",
            component: KnowledgeView,
            meta: { title: "知识中心" }
          },
          {
            path: "agent-config",
            name: "agent-config",
            component: AgentConfigurationView,
            meta: { title: "Agent 配置" }
          },
          {
            path: "integrations",
            name: "integrations",
            component: IntegrationsView,
            meta: { title: "集成中心" }
          },
          {
            path: "system",
            name: "system",
            component: SystemStatusView,
            meta: { title: "系统状态" }
          }
        ]
      },
      { path: "/:pathMatch(.*)*", redirect: "/incidents" }
    ]
  });

  let initialization: Promise<void> | null = null;
  router.beforeEach(async (to) => {
    initialization ??= auth.initialize();
    await initialization;
    const requiresAuth = to.matched.some((record) => record.meta.requiresAuth === true);
    const publicOnly = to.matched.some((record) => record.meta.publicOnly === true);
    if (requiresAuth && !auth.isAuthenticated()) {
      return { path: "/login", query: { redirect: to.fullPath } };
    }
    if (publicOnly && auth.isAuthenticated()) {
      return { path: "/incidents" };
    }
    return true;
  });

  return router;
}
