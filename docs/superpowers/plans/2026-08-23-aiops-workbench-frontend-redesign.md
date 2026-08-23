# AIOps Workbench Frontend Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Repository policy permits only one read-only plan-review subagent; implementation stays with the primary agent unless the user explicitly requests implementation subagents.

**Goal:** 将现有 Vue 3 通用 AI 工作台重构为事件驱动的 AIOps 运维工作台，并把现有 Chat Prompt/Skill 升级为可发布、可绑定、可审计的真实 Agent 配置。

**Architecture:** 保留现有 Vue 3、Pinia、Vue Router、typed API/SSE client 与 FastAPI/PostgreSQL 架构。前端先建立轻量设计系统和新的应用 Shell，再通过 owner-scoped Incident projection 驱动事件中心与调查工作台；Prompt/Skill 采用 resource/version/binding/audit 模型兼容迁移现有 Chat 配置，运行时在强制安全提示之外加载已发布配置快照。

**Tech Stack:** Vue 3.5、TypeScript strict、Pinia 3、Vue Router 4、Lucide Vue、原生 CSS、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL、Vitest、pytest、Ruff、Pyright、OpenSpec。

## Global Constraints

- 不迁移 React，不增加 PrimeVue、Element Plus、Naive UI 或第二套图标/状态管理依赖。
- 界面默认简体中文，保留技术名词；桌面与窄屏均可用并尊重 `prefers-reduced-motion`。
- 所有网络访问通过 typed API/SSE client；先改 `packages/api-contracts`，再同步后端和前端。
- 不展示或持久化模型隐藏 reasoning、原始异常、凭据、完整工具输出或 checkpoint 原始 state。
- Incident、Prompt、Skill、Binding、Audit、Knowledge 和 Diagnostic 全部从认证上下文执行 owner scope。
- Prompt/Skill 不能扩大系统 tool allowlist，不能绕过 Validator、Policy Gate 或恢复审批。
- 现有 Chat Prompt/Skill 数据必须兼容迁移，不能静默丢弃用户数据。
- PostgreSQL Agent 配置 migration 新增 revision `202608230002`，承接已发布的 Production Recovery revision `202608230001`，不得改写已发布 migration。
- 只使用真实 API 状态；loading、empty、error、partial、stale 和 permission denied 必须有明确界面。
- 实现遵循 TDD；每个任务通过聚焦测试后单独 Conventional Commit。

## Delivery Slices

本规格跨越三个可独立验收的子系统，按下列顺序交付：

1. **事件驱动工作台：** OpenSpec、共享契约、应用 Shell、Incident API、事件中心、调查工作台；
2. **Agent 配置闭环：** Prompt/Skill 版本化、绑定、运行快照、审计及配置 UI；
3. **统一工作区：** 运维助手、知识中心、集成中心、系统状态和最终视觉/响应式验收。

每个 Slice 完成时应用必须可构建、可运行，不允许长期保留只显示伪数据的页面。

---

### Task 1: 创建 OpenSpec 变更并锁定路由、Incident 与 Agent 配置契约

**Files:**
- Create: `openspec/changes/reframe-aiops-workbench/proposal.md`
- Create: `openspec/changes/reframe-aiops-workbench/design.md`
- Create: `openspec/changes/reframe-aiops-workbench/tasks.md`
- Create: `openspec/changes/reframe-aiops-workbench/specs/vue-app-shell/spec.md`
- Create: `openspec/changes/reframe-aiops-workbench/specs/aiops-incident-workspace/spec.md`
- Create: `openspec/changes/reframe-aiops-workbench/specs/chat-experience/spec.md`
- Create: `openspec/changes/reframe-aiops-workbench/specs/agent-configuration/spec.md`

**Interfaces:**
- Consumes: `openspec/specs/vue-app-shell/spec.md`, `openspec/specs/chat-experience/spec.md`, confirmed design spec.
- Produces: `/incidents`, `/incidents/:incidentId`, `/assistant`, `/knowledge`, `/agent-config`, `/integrations`, `/system`; owner-scoped Incident API; versioned Agent Configuration requirements.

- [ ] **Step 1: Write the delta requirements**

Use exact OpenSpec scenarios, including this route requirement:

```markdown
### Requirement: Event-first authenticated workspace
认证后的应用 SHALL 将 `/incidents` 作为默认首页，并 SHALL 提供事件中心、调查工作台、运维助手、知识中心、Agent 配置、集成中心和系统状态入口。

#### Scenario: Authenticated user opens root route
- **WHEN** 已认证用户访问 `/`
- **THEN** router MUST 导航到 `/incidents`，并在共享 Shell 中显示 owner-scoped Incident 队列。
```

Agent 配置 delta 必须定义 `draft -> published -> deprecated`、发布版本不可变、节点绑定、运行快照和审计；Chat delta 必须移除常驻 Prompt/Skill 侧栏与“无 focus outline”旧要求，改为 `:focus-visible`。

- [ ] **Step 2: Validate the new change**

Run: `openspec validate reframe-aiops-workbench --strict`

Expected: `reframe-aiops-workbench` validation succeeds with no missing Scenario.

- [ ] **Step 3: Commit**

```bash
git add openspec/changes/reframe-aiops-workbench
git commit -m "docs: specify event-first aiops workbench"
```

---

### Task 2: 扩展共享 Incident、运行状态和 Agent 配置契约

**Files:**
- Create: `packages/api-contracts/src/incidents.ts`
- Create: `packages/api-contracts/src/runtime-status.ts`
- Create: `packages/api-contracts/src/agent-configuration.ts`
- Modify: `packages/api-contracts/src/index.ts`
- Modify: `packages/api-contracts/src/openapi.ts`
- Test: `packages/api-contracts/tests/contracts.test.ts`

**Interfaces:**
- Consumes: existing `AiopsDiagnosticSummary`, `AiopsDiagnosticEvidenceChain`, `BackgroundJob`, response envelope.
- Produces: `IncidentSummary`, `IncidentDetail`, cursor-paginated Incident list, current `RecoveryIntent` projection, `RuntimeReadiness`, `AgentResource`, `AgentResourceVersion`, `AgentBinding`, server-derived Agent configuration capabilities, and mutation request/response types.

- [ ] **Step 1: Write failing contract tests**

Add compile/runtime fixtures that require these shapes:

```ts
const incident: IncidentSummary = {
  id: "incident_1",
  status: "active",
  alertName: "OrderPoolExhausted",
  service: "order-service",
  severity: "critical",
  firstSeenAt: "2026-08-23T08:00:00Z",
  lastSeenAt: "2026-08-23T08:05:00Z",
  deliveryCount: 3,
  diagnosticTaskId: "diagnostic_1",
  diagnosticStatus: "running",
  verificationStatus: "pending",
  currentStage: "investigation",
  source: "local-alertmanager",
  environment: null,
  assignee: null,
  agentMode: "multi",
  approvalStatus: null,
  recoveryMode: "automatic",
  recoveryExecutionStatus: "executing",
  recoveryIntentId: "intent_1"
};
expect(incident.currentStage).toBe("investigation");
```

Also assert `AgentNode` is the closed union `conversation | planner | replanner | investigator_runtime | investigator_log | investigator_change | adjudicator | validator | recovery_planner | report` and `AgentVersionStatus` is `draft | published | deprecated`. This maps to the current model-call roles; deterministic graph nodes such as `aggregator` and `policy_gate` are intentionally not configurable Prompt targets.

- [ ] **Step 2: Run tests to verify failure**

Run: `npm --workspace packages/api-contracts run test`

Expected: FAIL because the new modules and exported types do not exist.

- [ ] **Step 3: Add exact shared types and OpenAPI schemas**

Define immutable wire contracts such as:

```ts
export interface AgentResourceVersion {
  readonly id: string;
  readonly resourceId: string;
  readonly version: number;
  readonly status: "draft" | "published" | "deprecated";
  readonly content: string;
  readonly spec: Readonly<Record<string, unknown>>;
  readonly createdAt: string;
  readonly publishedAt: string | null;
}

export interface AgentBinding {
  readonly id: string;
  readonly node: AgentNode;
  readonly promptVersionId: string | null;
  readonly skillVersionIds: readonly string[];
  readonly updatedAt: string;
}
```

`RuntimeReadiness` reuses `/ready` dependency names and never exposes hostnames or secrets. Export all modules from `index.ts` and mirror them in `openapi.ts`.

`IncidentSummary` fields without a current fact source are nullable, never synthesized. Reuse the existing shared `RecoveryIntent` and `RecoveryStatus` contracts. `recoveryIntentId` is nullable; `recoveryExecutionStatus` is projected from the latest owner-scoped formal Intent when one exists and otherwise equals `not_available`. The response exposes `productionRecoveryExecution: true` only when the formal control plane is installed; it never infers execution from legacy Chat approval-request rows or Live Eval records.

`IncidentListResponse` uses opaque `nextCursor: string | null`. Ordering is stable by `(updatedAt DESC, id ASC)` and the cursor encodes only those public ordering fields through a server-owned serializer; clients must not construct or inspect it. Agent configuration responses include `capabilities.canManageConfiguration`. In the first local-workspace release every authenticated owner is administrator only of their own resources; cross-owner access remains uniformly denied. Hiding controls is presentation, while every mutation endpoint independently enforces the authenticated owner capability.

- [ ] **Step 4: Run contract checks**

Run: `npm run contracts:typecheck && npm --workspace packages/api-contracts run test`

Expected: both commands PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/api-contracts
git commit -m "feat: define workbench api contracts"
```

---

### Task 3: 提供 owner-scoped Incident 查询 API

**Files:**
- Modify: `apps/backend/src/super_ai/alert_ingestion/repositories.py`
- Modify: `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`
- Create: `apps/backend/src/super_ai/aiops/incident_routes.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/src/super_ai/memory/models.py` only if an existing timestamp/count field is not already mapped
- Test: `apps/backend/tests/test_aiops_incidents_api.py`

**Interfaces:**
- Consumes: authenticated `UserRecord`, `AlertIncidentModel`, `DiagnosticTaskModel`, `ProductionRecoveryIntentModel`, existing `AlertIncidentQueryRepository`.
- Produces: `GET /aiops/incidents`, `GET /aiops/incidents/{incident_id}` and `POST /aiops/incidents/{incident_id}:diagnose`.

- [ ] **Step 1: Write failing owner-scope and projection tests**

Create users A/B and incidents for both. Assert user A sees only A, detail lookup for B returns the same safe not-found response as an absent ID, and duplicate diagnose calls reuse the existing Diagnostic Task:

```python
response = await client.get("/aiops/incidents", headers=headers_a)
assert [item["id"] for item in response.json()["data"]["items"]] == ["incident_a"]

forbidden = await client.get("/aiops/incidents/incident_b", headers=headers_a)
assert forbidden.status_code == 404

first = await client.post("/aiops/incidents/incident_a:diagnose", headers=headers_a)
second = await client.post("/aiops/incidents/incident_a:diagnose", headers=headers_a)
assert first.json()["data"]["diagnosticTaskId"] == second.json()["data"]["diagnosticTaskId"]
```

Also create two historical formal Intents for one Diagnostic Task and assert the deterministic latest record is projected once without duplicating the Incident row. Assert the detail/list payload includes the current `recoveryIntentId`, `recoveryExecutionStatus` and `productionRecoveryExecution=true`; cross-owner Intents, legacy Chat approval rows and Live Eval execution keys never appear. Add at least three equal-timestamp Incidents and paginate with `limit=2`, asserting stable `(updated_at DESC, id ASC)` order, a non-null first `nextCursor`, a null terminal cursor and no duplicate/missing IDs.

- [ ] **Step 2: Run tests to verify failure**

Run from `apps/backend`: `uv run pytest tests/test_aiops_incidents_api.py -q`

Expected: FAIL with 404 for the unregistered Incident endpoints.

- [ ] **Step 3: Extend the repository projection without raw webhook payloads**

Expand `AlertIncidentRecord` and `list_owned(owner_user_id, status, limit, cursor)` to return only safe fields: source, first/last seen, delivery count, diagnostic task/status, report-derived Agent mode and recovery mode, verification status, pending approval status and the latest formal Recovery Intent ID/status. Join owner-scoped Diagnostic Task and `ProductionRecoveryIntentModel` data server-side. `environment` is derived only from allowlisted normalized labels when present; `assignee` remains `null` because no assignment subsystem exists. Legacy Chat approval-request rows and Live Eval execution keys are never projected as production Intents. Do not return normalized webhook JSON, trusted snapshots, credentials or internal checkpoint data.

- [ ] **Step 4: Add a focused router**

`create_incident_router(...)` must inject the current user and delegate diagnosis scheduling to the existing `IncidentDiagnosticScheduler`:

```python
@router.get("")
async def list_incidents(
    request: Request,
    user: Annotated[UserRecord, Depends(current_user)],
    status: Literal["active", "resolved", "all"] = "active",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> object:
    page = await repository.list_owned(
        owner_user_id=user.id, status=status, limit=limit, cursor=cursor
    )
    return success_response(
        request,
        {"items": [_incident_payload(item) for item in page.items], "nextCursor": page.next_cursor},
    )
```

Register the router during app creation; keep webhook ingestion routes separate.

- [ ] **Step 5: Run focused backend checks**

Run from `apps/backend`:

```bash
uv run pytest tests/test_aiops_incidents_api.py tests/test_chat_aiops_bridge.py tests/test_alert_ingestion_api.py -q
uv run ruff check src/super_ai/aiops/incident_routes.py src/super_ai/alert_ingestion tests/test_aiops_incidents_api.py
uv run pyright src/super_ai/aiops/incident_routes.py src/super_ai/alert_ingestion
```

Expected: all commands PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/src/super_ai/aiops/incident_routes.py apps/backend/src/super_ai/alert_ingestion apps/backend/src/super_ai/api/app.py apps/backend/tests/test_aiops_incidents_api.py
git commit -m "feat: expose owner scoped aiops incidents"
```

---

### Task 4: 建立视觉 tokens、可访问 primitives 和事件优先应用 Shell

**Files:**
- Modify: `apps/frontend/src/styles.css`
- Modify: `apps/frontend/src/foundation.ts`
- Create: `apps/frontend/src/ui/AppButton.vue`
- Create: `apps/frontend/src/ui/AppBadge.vue`
- Create: `apps/frontend/src/ui/AppDrawer.vue`
- Create: `apps/frontend/src/ui/AppTabs.vue`
- Create: `apps/frontend/src/ui/AppSkeleton.vue`
- Modify: `apps/frontend/src/layouts/WorkspaceLayout.vue`
- Modify: `apps/frontend/src/components/WorkspaceNavigation.vue`
- Modify: `apps/frontend/src/router/index.ts`
- Create: `apps/frontend/src/views/IncidentCenterView.vue`
- Create: `apps/frontend/src/views/IncidentWorkspaceView.vue`
- Create: `apps/frontend/src/views/AgentConfigurationView.vue`
- Create: `apps/frontend/src/views/IntegrationsView.vue`
- Create: `apps/frontend/src/views/SystemStatusView.vue`
- Test: `apps/frontend/tests/designSystem.test.ts`
- Modify: `apps/frontend/tests/appShellRouter.test.ts`
- Modify: `apps/frontend/tests/appShellComponents.test.ts`

**Interfaces:**
- Consumes: existing feedback/loading/error components and auth/chat stores.
- Produces: seven-route Shell, design tokens and reusable accessible primitives.

- [ ] **Step 1: Write failing Shell and primitive tests**

Assert authenticated `/` redirects to `/incidents`, all seven labels are present, active matching supports nested Incident routes, drawer restores focus, tabs expose ARIA state, and every interactive primitive has visible keyboard focus.

```ts
await router.push("/");
await router.isReady();
expect(router.currentRoute.value.path).toBe("/incidents");
expect(wrapper.get('[aria-current="page"]').text()).toContain("事件中心");
expect(wrapper.get('[role="tab"][aria-selected="true"]').exists()).toBe(true);
```

- [ ] **Step 2: Run tests to verify failure**

Run: `npm --workspace apps/frontend run test -- designSystem.test.ts appShellRouter.test.ts appShellComponents.test.ts`

Expected: FAIL because routes and primitives are absent.

- [ ] **Step 3: Define the token contract**

Replace scattered foundations with semantic tokens, including:

```css
:root {
  --surface-canvas: #f3f2ef;
  --surface-panel: #fbfbf9;
  --surface-raised: #ffffff;
  --nav-bg: #18211f;
  --text-primary: #18201e;
  --text-secondary: #56605c;
  --accent: #147d64;
  --info: #2767b2;
  --warning: #a76512;
  --danger: #b93838;
  --focus-ring: 0 0 0 3px rgb(39 103 178 / 28%);
  --radius-control: 0.5rem;
  --radius-panel: 0.75rem;
}

:where(a, button, input, textarea, select, [tabindex]):focus-visible {
  outline: 2px solid var(--info);
  outline-offset: 2px;
}
```

Remove Chat rules that suppress focus outlines. Add reduced-motion rules.

- [ ] **Step 4: Implement focused primitives**

Each primitive accepts typed variants and forwards native accessibility attributes. `AppDrawer` traps neither content nor scrolling when closed, closes on Escape, uses `role="dialog" aria-modal="true"`, and restores focus to its trigger.

- [ ] **Step 5: Rebuild the Shell and route map**

Use these routes and redirects:

```ts
{ path: "/", redirect: "/incidents" },
{ path: "incidents", name: "incidents", component: IncidentCenterView },
{ path: "incidents/:incidentId", name: "incident-workspace", component: IncidentWorkspaceView },
{ path: "assistant", name: "assistant", component: ChatView },
{ path: "knowledge", name: "knowledge", component: KnowledgeView },
{ path: "agent-config", name: "agent-config", component: AgentConfigurationView },
{ path: "integrations", name: "integrations", component: IntegrationsView },
{ path: "system", name: "system", component: SystemStatusView }
```

Keep temporary redirects `/chat -> /assistant`, `/aiops -> /incidents`, `/mcp -> /integrations`. The first Shell commit must remain functional: `IncidentCenterView` composes the existing AIOps alert/history components, `IntegrationsView` composes the existing MCP management view, `AgentConfigurationView` composes the existing Chat Prompt/Skill controls, and `SystemStatusView` consumes the real `/health` result. Subsequent tasks replace these compatibility compositions with the approved specialized experiences.

- [ ] **Step 6: Run frontend checks**

Run:

```bash
npm --workspace apps/frontend run test -- designSystem.test.ts appShellRouter.test.ts appShellComponents.test.ts workspaceLayout.test.ts
npm run frontend:typecheck
```

Expected: all commands PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src apps/frontend/tests
git commit -m "feat: add event first workbench shell"
```

---

### Task 5: 实现真实事件中心

**Files:**
- Create: `apps/frontend/src/incidents/incidentClient.ts`
- Create: `apps/frontend/src/stores/incidents.ts`
- Create: `apps/frontend/src/components/incidents/IncidentMetrics.vue`
- Create: `apps/frontend/src/components/incidents/IncidentQueue.vue`
- Create: `apps/frontend/src/components/incidents/IncidentPreview.vue`
- Modify: `apps/frontend/src/views/IncidentCenterView.vue`
- Test: `apps/frontend/tests/incidentClient.test.ts`
- Test: `apps/frontend/tests/incidentStore.test.ts`
- Test: `apps/frontend/tests/incidentCenter.test.ts`

**Interfaces:**
- Consumes: `GET /aiops/incidents`, `POST /aiops/incidents/{id}:diagnose`, Task 2 contracts and Task 4 primitives.
- Produces: selected Incident state, status/severity filters, diagnosis launch and `/incidents/:id` navigation.

- [ ] **Step 1: Write failing client/store/component tests**

Use a fake typed client with active, pending-approval and resolved incidents. Assert filters do not mutate server data, selection drives the preview, empty/error/retry states render, and diagnose uses the real ID.

```ts
await store.initialize();
store.setStatusFilter("active");
expect(store.visibleIncidents.map((item) => item.id)).toEqual(["incident_critical"]);
await store.startDiagnostic("incident_critical");
expect(fake.diagnosedIds).toEqual(["incident_critical"]);
```

- [ ] **Step 2: Run tests to verify failure**

Run: `npm --workspace apps/frontend run test -- incidentClient.test.ts incidentStore.test.ts incidentCenter.test.ts`

Expected: FAIL because Incident client/store/components do not exist.

- [ ] **Step 3: Implement typed data flow**

`incidentClient.ts` only calls the typed API client. Pinia owns `items`, `selectedId`, filters, loading/error/stale timestamps and derived metrics. The view must not synthesize counts not derivable from the returned list. Pending approvals come from the server projection. “自动恢复执行中” displays “未启用” when `productionRecoveryExecution=false`; 24-hour safety rate displays “暂无数据” until a persisted metric endpoint exists.

- [ ] **Step 4: Implement master-detail UI**

Desktop uses queue + preview; narrow screens open preview in a drawer. Each row includes textual severity/status/current stage, source/service, elapsed time, agent mode when available, and recovery mode. Use native buttons/links with accessible names; do not make a non-interactive `<div>` clickable.

- [ ] **Step 5: Run focused frontend checks**

Run:

```bash
npm --workspace apps/frontend run test -- incidentClient.test.ts incidentStore.test.ts incidentCenter.test.ts
npm run frontend:typecheck
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/incidents apps/frontend/src/stores/incidents.ts apps/frontend/src/components/incidents apps/frontend/src/views/IncidentCenterView.vue apps/frontend/tests/incident*
git commit -m "feat: build real incident center"
```

---

### Task 6: 将现有 AIOps 诊断页重构为调查工作台

**Files:**
- Modify: `apps/frontend/src/aiops/aiopsClient.ts`
- Modify: `apps/frontend/src/stores/aiops.ts`
- Create: `apps/frontend/src/recovery/recoveryClient.ts`
- Create: `apps/frontend/src/stores/recovery.ts`
- Create: `apps/frontend/src/components/investigation/InvestigationHeader.vue`
- Create: `apps/frontend/src/components/investigation/ExecutionTrace.vue`
- Create: `apps/frontend/src/components/investigation/HypothesisEvidencePanel.vue`
- Create: `apps/frontend/src/components/investigation/ToolAuditPanel.vue`
- Create: `apps/frontend/src/components/investigation/RecoveryClosurePanel.vue`
- Create: `apps/frontend/src/components/investigation/InvestigationContextAside.vue`
- Modify: `apps/frontend/src/views/IncidentWorkspaceView.vue`
- Retain/refactor: `apps/frontend/src/components/AiopsEvidenceChain.vue`
- Retain/refactor: `apps/frontend/src/components/AiopsTimeline.vue`
- Retain/refactor: `apps/frontend/src/components/AiopsReportPanel.vue`
- Test: `apps/frontend/tests/investigationWorkspace.test.ts`
- Create: `apps/frontend/tests/recoveryClient.test.ts`
- Create: `apps/frontend/tests/recoveryStore.test.ts`
- Modify: `apps/frontend/tests/aiopsComponents.test.ts`
- Modify: `apps/frontend/tests/aiopsStore.test.ts`

**Interfaces:**
- Consumes: `IncidentDetail`, existing `AiopsDiagnosticEvidenceChain`, diagnostic SSE/report payload, and the existing owner-scoped formal Recovery Intent API.
- Produces: six-tab investigation workspace, explicit safety/degradation presentation, formal Intent status/events, and owner actions permitted by the existing recovery state machine. The frontend never derives action/target/arguments and never calls an executor directly.

- [ ] **Step 1: Write failing semantic presentation tests**

Fixtures must cover: running Single-Agent, running Multi-Agent with one inconclusive Specialist, deterministic grounded fallback, automatic Compose Intent, PostgreSQL `awaiting_approval`, executing/verifying/recovered, verification failure and manual intervention. Client/store tests prove duplicate create returns the same formal Intent, cross-owner IDs remain hidden by the API, approval confirmation binds the Incident ID, and UI code never submits action, target, PID, SQL, Compose path or execution arguments.

Recovery Store tests use fake timers and document visibility changes. They prove `queued/revalidating/executing/verifying` poll the formal Intent and events at a bounded 2-second interval only while the page is visible; `recovered/denied/rejected/expired/cancelled/verification_failed/manual_intervention` stop polling; hidden pages pause and resume; transient failures retain the last successful Intent, mark it stale and expose retry without converting the state to success. Incident Store tests prove `loadMore()` follows opaque `nextCursor` without duplicating rows.

```ts
expect(wrapper.get('[data-status="inconclusive"]').text()).toContain("缺少独立日志证据");
expect(wrapper.get('[data-validator="deterministic_grounded_fallback"]').text())
  .toContain("语义核验不可用，确定性证据通过，已转人工复核");
expect(wrapper.get('[data-recovery-permitted="false"]').text()).toContain("禁止自动执行");
```

- [ ] **Step 2: Run tests to verify failure**

Run from repository root: `npm --workspace apps/frontend run test -- investigationWorkspace.test.ts recoveryClient.test.ts recoveryStore.test.ts aiopsComponents.test.ts aiopsStore.test.ts`

Expected: frontend FAIL because the semantic adapters, formal recovery client/store and new workspace are absent. Existing backend recovery API contract tests remain green and are not reimplemented here.

- [ ] **Step 3: Add a public result adapter**

Create pure typed selectors in the AIOps store that map existing `resultPayload`, steps, evidence and report payload to public UI states. Unknown fields remain “未知/未提供”; never infer success from missing values and never render raw payload JSON by default.

- [ ] **Step 4: Implement six-tab workspace**

Tabs are overview, trace, hypothesis/evidence, tool audit, recovery closure and audit timeline. The context aside shows owner-safe impact, mode, configuration version IDs and checkpoint metadata counts—not checkpoint state. Existing working evidence/report components are wrapped or split instead of duplicated.

The recovery tab reads the formal Intent projected by Incident detail and loads its append-only public events. Eligible Compose Intents may progress automatically through `queued -> revalidating -> executing -> verifying -> recovered`; the UI shows status and verification but provides no execute button. PostgreSQL Intents at `awaiting_approval` expose explicit approve/reject controls with Incident ID confirmation and risk copy. `manual_intervention` and `verification_failed` expose audit context without automatic retry. Legacy Chat approval requests and Live Eval execution keys remain read-only evidence and are never rendered as formal Intents.

The Recovery Store owns bounded refresh independently from diagnostic SSE. It polls only non-terminal formal Intents every 2 seconds while `document.visibilityState === "visible"`, fetches events with the last durable `afterSequence`, pauses when hidden, resumes immediately when visible, and stops at every terminal status or view disposal. A failed refresh keeps the last known state with `stale=true` and an explicit retry action; it never invents a recovery transition.

- [ ] **Step 5: Preserve SSE and cancellation behavior**

Route opening an active Diagnostic resumes the existing stream. Cancel continues to call the Background Job endpoint. Refresh reconciles persisted task/chain before applying new events.

- [ ] **Step 6: Run focused checks**

Run:

```bash
npm --workspace apps/frontend run test -- investigationWorkspace.test.ts recoveryClient.test.ts recoveryStore.test.ts aiopsComponents.test.ts aiopsStore.test.ts activeAlerts.test.ts
npm run frontend:typecheck
cd apps/backend && uv run pytest tests/test_recovery_api.py tests/test_recovery_api_security.py tests/test_chat_aiops_bridge.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/aiops apps/frontend/src/recovery apps/frontend/src/stores/aiops.ts apps/frontend/src/stores/recovery.ts apps/frontend/src/components/investigation apps/frontend/src/views/IncidentWorkspaceView.vue apps/frontend/src/components/Aiops* apps/frontend/tests
git commit -m "feat: redesign incident investigation workspace"
```

---

### Task 7: 将现有 Chat Prompt/Skill 兼容迁移为版本化 Agent 配置

**Files:**
- Create: `apps/backend/src/super_ai/agent_configuration/__init__.py`
- Create: `apps/backend/src/super_ai/agent_configuration/domain.py`
- Create: `apps/backend/src/super_ai/agent_configuration/service.py`
- Create: `apps/backend/src/super_ai/agent_configuration/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/chat_runs_sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Create: `apps/backend/src/super_ai/memory/agent_configuration_sqlalchemy.py`
- Create: `apps/backend/alembic/versions/202608230002_add_agent_configuration_versions.py`
- Test: `apps/backend/tests/test_agent_configuration_migration.py`
- Test: `apps/backend/tests/test_agent_configuration_service.py`
- Test: `apps/backend/tests/test_agent_configuration_repository.py`

**Interfaces:**
- Consumes: existing `user_chat_prompts`, `user_chat_skills`, `user_chat_configurations` and validation rules.
- Produces: `AgentResourceRecord`, `AgentVersionRecord`, `AgentBindingRecord`, `AgentConfigurationSnapshot`, repositories, publish/bind service and server-owned configuration snapshot fields on Chat Run/Diagnostic Task.

- [ ] **Step 1: Write failing domain and PostgreSQL tests**

Cover immutable publish, one draft per resource, monotonic version numbers, node binding to published versions only, owner isolation, concurrent publish conflict, server-owned Run snapshot persistence, retry reuse of the original snapshot, and compatibility import of existing Chat data.

```python
published = await service.publish_version(owner_user_id="owner_a", version_id=draft.id)
assert published.status == "published"
with pytest.raises(PublishedVersionImmutable):
    await service.update_draft(owner_user_id="owner_a", version_id=published.id, content="changed")
with pytest.raises(ConfigurationNotFound):
    await service.get_resource(owner_user_id="owner_b", resource_id=published.resource_id)
```

- [ ] **Step 2: Run tests to verify failure**

Run from `apps/backend`:

`uv run pytest tests/test_agent_configuration_service.py tests/test_agent_configuration_repository.py tests/test_agent_configuration_migration.py -q`

Expected: FAIL because the domain, tables and migration do not exist.

- [ ] **Step 3: Define focused domain types**

Use closed literals and immutable records:

```python
AgentNode = Literal[
    "conversation", "planner", "replanner", "investigator_runtime",
    "investigator_log", "investigator_change", "adjudicator", "validator",
    "recovery_planner", "report",
]
ResourceKind = Literal["prompt", "skill"]
VersionStatus = Literal["draft", "published", "deprecated"]

@dataclass(frozen=True, slots=True)
class AgentConfigurationSnapshot:
    node: AgentNode
    prompt_version_id: str | None
    prompt_content: str | None
    skill_version_ids: tuple[str, ...]
    skills: tuple[PublishedSkill, ...]
```

- [ ] **Step 4: Add normalized tables and safe migration**

Create `agent_config_resources`, `agent_config_versions`, `agent_config_bindings`, and `agent_config_audit_events`. Add non-null JSONB `agent_configuration_snapshot` with `{}` default to `chat_agent_runs` and `aiops_diagnostic_tasks`; clients cannot submit these fields. `spec` is bounded JSONB; content remains Text. Add owner/resource/version unique constraints and binding node uniqueness.

The data migration creates one published version for each existing Prompt/Skill and maps the existing Chat selection to the `conversation` binding. Imported Skill versions preserve Markdown but receive safe compatibility metadata: `imported=true`, `allowedTools=[]`, `risk="read_only"`, `inputSchema={}`, `outputSchema={}`, bounded default timeout, no automatic retry, and Conversation-only binding eligibility. Existing tables remain during compatibility period.

- [ ] **Step 5: Implement transaction-safe repositories and service**

Publish uses PostgreSQL uniqueness and conflict-safe reread. Binding validates that every version belongs to the same owner, has the expected kind, is `published`, and is eligible for the requested mapped node. Audit events are append-only with stable event IDs. Skill tool permissions are intersected with the system node allowlist, never unioned. The stable node mapping is explicit: AIOps `investigator` calls resolve by Specialist source to one of the three investigator nodes; `planner`, `replanner`, `adjudicator`, `validator`, `recovery_planner`, and `report` map one-to-one; deterministic nodes have no configurable Prompt binding.

- [ ] **Step 6: Run migration and focused checks**

Run from `apps/backend`:

```bash
uv run alembic upgrade head
uv run pytest tests/test_agent_configuration_service.py tests/test_agent_configuration_repository.py tests/test_agent_configuration_migration.py -q
uv run ruff check src/super_ai/agent_configuration src/super_ai/memory/agent_configuration_sqlalchemy.py alembic/versions/202608230002_add_agent_configuration_versions.py tests/test_agent_configuration_*.py
uv run pyright src/super_ai/agent_configuration src/super_ai/memory/agent_configuration_sqlalchemy.py
```

Expected: migration reaches `202608230002`; all checks PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/src/super_ai/agent_configuration apps/backend/src/super_ai/memory apps/backend/alembic/versions/202608230002_add_agent_configuration_versions.py apps/backend/tests/test_agent_configuration_*
git commit -m "feat: version agent prompts and skills"
```

---

### Task 8: 添加 Agent 配置 API、运行时快照与审计

**Files:**
- Create: `apps/backend/src/super_ai/agent_configuration/routes.py`
- Create: `apps/backend/src/super_ai/agent_configuration/runtime.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/src/super_ai/chat/configuration.py`
- Modify: `apps/backend/src/super_ai/chat/runs.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Test: `apps/backend/tests/test_agent_configuration_api.py`
- Test: `apps/backend/tests/test_agent_configuration_runtime.py`
- Modify: `apps/backend/tests/test_stream_rag_chat_api.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`

**Interfaces:**
- Consumes: Task 7 service and snapshot, current Chat/AIOps mandatory prompts and tool policies.
- Produces: CRUD/draft/publish/bind/audit endpoints; runtime `resolve_snapshot(owner_user_id, node)` integration.

- [ ] **Step 1: Write failing API, safety and runtime tests**

Cover create draft, update draft, validate, publish, bind, list audit, cross-owner denial, published edit rejection, wrong-kind binding, untrusted Skill tool expansion and fallback when no binding exists.

```python
snapshot = await runtime.resolve_snapshot(owner_user_id="owner_a", node="validator")
assembled = runtime.assemble_system_prompt(MANDATORY_VALIDATOR_PROMPT, snapshot)
assert assembled.startswith(MANDATORY_VALIDATOR_PROMPT)
assert set(snapshot.allowed_tools) <= VALIDATOR_SYSTEM_TOOL_ALLOWLIST
assert snapshot.policy_gate_required is True
assert snapshot.owner_user_id == "owner_a"
```

- [ ] **Step 2: Run tests to verify failure**

Run from `apps/backend`:

`uv run pytest tests/test_agent_configuration_api.py tests/test_agent_configuration_runtime.py -q`

Expected: FAIL because routes and runtime resolver are absent.

- [ ] **Step 3: Implement owner-scoped routes**

Expose:

```text
GET    /agent-configuration/resources
POST   /agent-configuration/resources
GET    /agent-configuration/resources/{resource_id}
POST   /agent-configuration/resources/{resource_id}/versions
PUT    /agent-configuration/versions/{version_id}
POST   /agent-configuration/versions/{version_id}:validate
POST   /agent-configuration/versions/{version_id}:publish
GET    /agent-configuration/bindings
PUT    /agent-configuration/bindings/{node}
GET    /agent-configuration/audit-events
```

All errors map to stable catalog codes; raw validation/parser exceptions stay server-side. Validation may warn on suspicious policy-bypass prose, but prose filtering is not an authorization boundary. Runtime assembly places published content in a delimited untrusted configuration section; mandatory safety, owner scope, tool intersection, Validator routing and Policy Gate execution remain server-owned fields that configuration cannot disable.

- [ ] **Step 4: Assemble runtime configuration behind mandatory safety text**

For each node, resolve one immutable snapshot at Run/Diagnostic creation and persist the server-generated resource/version ID map in `agent_configuration_snapshot`. Chat idempotent request reuse and Background Job retry must read the stored snapshot instead of resolving current bindings again. Diagnostic retries and LangGraph checkpoint recovery follow the same rule. Public payloads expose only version IDs/node names, never Prompt content. Prompt composition order is: mandatory system safety -> published node prompt -> published Skill catalog. Configuration changes do not affect an already running task.

- [ ] **Step 5: Preserve old Chat APIs as compatibility adapters**

`/chat/configuration`, `/chat/prompts`, and `/chat/skills` delegate to the `conversation` resource/binding service and retain current response shapes until the new Agent Configuration UI is stable. Add deprecation comments but do not remove the endpoints in this change.

- [ ] **Step 6: Run focused backend regression**

Run from `apps/backend`:

```bash
uv run pytest tests/test_agent_configuration_api.py tests/test_agent_configuration_runtime.py tests/test_agent_configuration_repository.py tests/test_chat_runs_repository.py tests/test_stream_rag_chat_api.py tests/test_aiops_network_resume.py tests/test_aiops_v4_workflow.py tests/test_chat_execution_policy.py -q
uv run ruff check src/super_ai/agent_configuration src/super_ai/chat/configuration.py
uv run pyright src/super_ai/agent_configuration src/super_ai/chat/configuration.py
```

Expected: PASS; existing Chat assembly behavior remains compatible.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/src/super_ai/agent_configuration apps/backend/src/super_ai/api/app.py apps/backend/src/super_ai/chat apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests
git commit -m "feat: bind versioned agent configuration at runtime"
```

---

### Task 9: 构建 Agent 配置中心并移除 Chat 常驻编辑器

**Files:**
- Create: `apps/frontend/src/agentConfiguration/agentConfigurationClient.ts`
- Create: `apps/frontend/src/stores/agentConfiguration.ts`
- Create: `apps/frontend/src/components/agentConfiguration/AgentNodeList.vue`
- Create: `apps/frontend/src/components/agentConfiguration/ResourceLibrary.vue`
- Create: `apps/frontend/src/components/agentConfiguration/VersionEditor.vue`
- Create: `apps/frontend/src/components/agentConfiguration/BindingPanel.vue`
- Create: `apps/frontend/src/components/agentConfiguration/ConfigurationAudit.vue`
- Modify: `apps/frontend/src/views/AgentConfigurationView.vue`
- Modify: `apps/frontend/src/views/ChatView.vue`
- Remove after replacement: `apps/frontend/src/components/ChatPromptSidebar.vue`
- Remove after replacement: `apps/frontend/src/components/ChatSkillSidebar.vue`
- Test: `apps/frontend/tests/agentConfigurationClient.test.ts`
- Test: `apps/frontend/tests/agentConfigurationStore.test.ts`
- Test: `apps/frontend/tests/agentConfigurationView.test.ts`
- Modify: `apps/frontend/tests/chatAssemblySettings.test.ts`
- Modify: `apps/frontend/tests/chatLayout.test.ts`

**Interfaces:**
- Consumes: Task 2 contracts and Task 8 endpoints.
- Produces: draft/edit/validate/publish/bind/rollback UI and a conversation Run details link to exact configuration versions.

- [ ] **Step 1: Write failing workflow tests**

Assert published content is read-only, editing published creates a new draft, publish requires validation, binding lists only published compatible versions, rollback rebinds a historical version, and Chat has no Prompt/Skill sidebars.

```ts
await wrapper.get('[aria-label="发布版本 3"]').trigger("click");
expect(fake.publishedVersionIds).toEqual(["version_3"]);
await wrapper.get('[aria-label="绑定到 Validator"]').trigger("click");
expect(fake.bindings.at(-1)).toMatchObject({ node: "validator", promptVersionId: "version_3" });
expect(chatWrapper.findComponent({ name: "ChatPromptSidebar" }).exists()).toBe(false);
```

- [ ] **Step 2: Run tests to verify failure**

Run: `npm --workspace apps/frontend run test -- agentConfigurationClient.test.ts agentConfigurationStore.test.ts agentConfigurationView.test.ts chatAssemblySettings.test.ts chatLayout.test.ts`

Expected: FAIL because the Agent Configuration UI does not exist and Chat still owns editors.

- [ ] **Step 3: Implement typed client and Pinia state machine**

Store state distinguishes library loading, selected resource/version, dirty draft, validation result, publish state, binding state, server-derived `canManageConfiguration` and audit pagination. Route changes with dirty content require `ConfirmDialog`; save errors keep the draft visible and never imply publication. The first release treats each authenticated owner as administrator of only their own local-workspace resources; mutation authorization is enforced server-side even when the UI control is hidden.

- [ ] **Step 4: Implement three-pane desktop and drawer-based narrow UI**

Desktop uses node/resource navigation, version editor and binding/audit context. Narrow screens progressively disclose the editor and binding drawer. Prompt editor displays variables and output Schema; Skill editor displays tool allowlist, risk, timeouts and idempotency requirements.

- [ ] **Step 5: Simplify Chat**

Remove both configuration sidebars. Add a compact Run details drawer that shows Intent, model, memory, tool activity, citations and exact Prompt/Skill version IDs; add a link to `/agent-config?node=conversation` for authorized editing.

- [ ] **Step 6: Run focused frontend checks**

Run:

```bash
npm --workspace apps/frontend run test -- agentConfigurationClient.test.ts agentConfigurationStore.test.ts agentConfigurationView.test.ts chatAssemblySettings.test.ts chatLayout.test.ts chatComponents.test.ts
npm run frontend:typecheck
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/agentConfiguration apps/frontend/src/stores/agentConfiguration.ts apps/frontend/src/components/agentConfiguration apps/frontend/src/views/AgentConfigurationView.vue apps/frontend/src/views/ChatView.vue apps/frontend/src/components/ChatPromptSidebar.vue apps/frontend/src/components/ChatSkillSidebar.vue apps/frontend/tests
git commit -m "feat: add agent configuration center"
```

---

### Task 10: 统一运维助手、知识中心、集成中心和系统状态

**Files:**
- Modify: `apps/frontend/src/views/ChatView.vue`
- Modify: `apps/frontend/src/views/KnowledgeView.vue`
- Modify/refactor: `apps/frontend/src/views/McpView.vue`
- Modify: `apps/frontend/src/views/IntegrationsView.vue`
- Modify: `apps/frontend/src/views/SystemStatusView.vue`
- Create: `apps/frontend/src/runtime/runtimeStatusClient.ts`
- Create: `apps/frontend/src/stores/runtimeStatus.ts`
- Create: `apps/frontend/src/components/runtime/DependencyStatusList.vue`
- Create: `apps/frontend/src/components/runtime/BackgroundJobList.vue`
- Create: `apps/frontend/src/components/runtime/EvaluationSummary.vue`
- Modify: `apps/frontend/src/runtimeHealth.ts`
- Test: `apps/frontend/tests/runtimeStatusClient.test.ts`
- Test: `apps/frontend/tests/systemStatusView.test.ts`
- Modify: `apps/frontend/tests/chatComponents.test.ts`
- Modify: `apps/frontend/tests/knowledgeComponents.test.ts`
- Modify: `apps/frontend/tests/appShellTransport.test.ts`

**Interfaces:**
- Consumes: existing `/health`, `/ready`, `/config/check`, `/background-jobs`, MCP endpoints, knowledge stores, evaluation history when available.
- Produces: real Integration/System views and visually unified Assistant/Knowledge views.

- [ ] **Step 1: Write failing transport and state tests**

Assert process health and readiness are distinct, Redis degradation is non-blocking when the backend says so, secrets never render, background job failure is visible, and missing Eval history renders “暂无已保存结果” rather than zero.

```ts
expect(wrapper.get('[data-capability="api-process"]').text()).toContain("进程在线");
expect(wrapper.get('[data-capability="full-runtime"]').text()).toContain("依赖降级");
expect(wrapper.text()).not.toContain("sk-");
expect(wrapper.get('[data-eval="live"]').text()).toContain("暂无已保存结果");
```

- [ ] **Step 2: Run tests to verify failure**

Run: `npm --workspace apps/frontend run test -- runtimeStatusClient.test.ts systemStatusView.test.ts appShellTransport.test.ts`

Expected: FAIL because runtime-status transport and pages are absent.

- [ ] **Step 3: Implement real status composition**

Call `/health` for liveness, `/ready` for dependencies, `/config/check` for safe configuration validity and `/background-jobs` for owner jobs. Do not poll faster than 30 seconds; pause polling while the document is hidden; keep the last successful timestamp and display stale state after 90 seconds.

- [ ] **Step 4: Migrate MCP into Integration Center**

Reuse existing MCP Store/client/forms. Group cards by alert/metrics, logs/retrieval, model, notification and data infrastructure. Connection cards show only safe server summaries and retain check/discover/enable/disable actions.

- [ ] **Step 5: Restyle Assistant and Knowledge without changing behavior**

Assistant keeps persisted runs, SSE, citations, pending actions and memory controls. Knowledge keeps upload, chunk preview, index polling and retrieval traces. Replace generic copy with AIOps examples and use shared primitives/states without altering API behavior.

- [ ] **Step 6: Add persisted Eval summary only through a real endpoint**

If the existing API exposes evaluation history, type and consume it. If it is CLI/repository-only, add a focused authenticated read-only endpoint and contract test in this same step; return persisted run summaries only, never fixture ground truth. The empty response remains valid and renders the explicit empty state.

- [ ] **Step 7: Run focused checks**

Run:

```bash
npm --workspace apps/frontend run test -- runtimeStatusClient.test.ts systemStatusView.test.ts appShellTransport.test.ts chatComponents.test.ts knowledgeComponents.test.ts
npm run frontend:typecheck
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/frontend/src apps/frontend/tests packages/api-contracts apps/backend/src/super_ai/api apps/backend/tests
git commit -m "feat: unify aiops workspace surfaces"
```

---

### Task 11: 完成全局回归、响应式视觉验收和文档同步

**Files:**
- Modify: `openspec/changes/reframe-aiops-workbench/tasks.md`
- Modify: `openspec/specs/vue-app-shell/spec.md` only when archiving the completed change
- Modify: `openspec/specs/chat-experience/spec.md` only when archiving the completed change
- Create/Modify: `openspec/specs/aiops-incident-workspace/spec.md` when archiving
- Create/Modify: `openspec/specs/agent-configuration/spec.md` when archiving
- Modify: `docs/index.md` or relevant VitePress page describing the current workbench
- Store screenshots outside generated `docs/.vitepress/dist/`; use the repository's documented UI evidence location if present

**Interfaces:**
- Consumes: all earlier tasks.
- Produces: verified implementation, synchronized specs/docs and desktop/narrow visual evidence.

- [ ] **Step 1: Run focused frontend regression**

Run:

```bash
npm run contracts:typecheck
npm --workspace packages/api-contracts run test
npm run frontend:typecheck
npm run frontend:test
npm run frontend:build
```

Expected: all commands exit 0; no unconditional skips or loosened assertions.

- [ ] **Step 2: Run focused backend regression**

Run from `apps/backend`:

```bash
uv run pytest tests/test_aiops_incidents_api.py tests/test_agent_configuration_api.py tests/test_agent_configuration_runtime.py tests/test_agent_configuration_repository.py tests/test_stream_rag_chat_api.py tests/test_aiops_v4_workflow.py tests/test_alert_ingestion_api.py -q
uv run ruff check .
uv run pyright
```

Expected: all commands PASS. Full backend pytest is not required unless the focused run reveals cross-cutting regressions.

- [ ] **Step 3: Run OpenSpec and docs validation**

Run:

```bash
openspec validate --all
npm run docs:build
```

Expected: both commands PASS and no generated `docs/.vitepress/dist` changes are staged.

- [ ] **Step 4: Perform real API/UI smoke acceptance**

Start the existing local stack and verify with a real registered test user:

```text
Desktop 1440x900:
- root opens Incident Center;
- active Incident opens Investigation Workspace;
- Prompt draft can validate, publish and bind;
- new Chat Run records the bound version IDs;
- System Status distinguishes health from readiness.

Narrow 390x844:
- navigation opens as drawer;
- Incident preview is readable without horizontal scrolling;
- recovery approval remains explicit;
- Agent editor warns before discarding a dirty draft.
```

Capture one desktop Incident Center, one desktop Investigation Workspace, one Agent Configuration and one narrow Incident screenshot. Inspect them for clipping, tiny text, raw payload leaks and focus visibility.

- [ ] **Step 5: Update tasks/specs/docs and archive the change**

Mark only verified OpenSpec tasks complete, synchronize WIKI with the repository workflow, archive `reframe-aiops-workbench`, rerun `openspec validate --all`, and describe the actual implemented state rather than the design intent.

- [ ] **Step 6: Commit**

```bash
git add openspec docs apps/frontend packages/api-contracts apps/backend
git commit -m "docs: complete aiops workbench rollout"
```

## Self-Review

- Spec coverage: Tasks 1–6 cover event-first IA, Shell, Incident Center and Investigation Workspace; Tasks 7–9 cover Prompt/Skill versioning, binding, runtime loading, audit and UI; Task 10 covers Assistant, Knowledge, Integrations, System Status and Eval; Task 11 covers responsive/accessibility/visual verification.
- Reuse coverage: existing Vue/Pinia/Router/Lucide, typed clients, stores, Incident repository, diagnostic scheduler, Chat compatibility endpoints, AIOps chain, MCP and readiness APIs are reused; no new UI dependency is added.
- Safety coverage: owner isolation, immutable published versions, mandatory prompt precedence, tool allowlist intersection, secret-safe status and no hidden reasoning are tested.
- Migration coverage: existing Chat Prompt/Skill/configuration data is migrated and old endpoints remain compatibility adapters during this change.
- Acceptance coverage: contracts, frontend, focused backend, Ruff, Pyright, OpenSpec, docs build and real desktop/narrow smoke checks are explicit.
