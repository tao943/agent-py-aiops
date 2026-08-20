# Order API Pool Leak Multi-Agent Live Scenario Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增可重复注入、真实采集 Runtime 与 order-api 日志、可安全恢复并支持公平 Single/Multi A/B 的 `APY-LIVE-ORDER-POOL-LEAK-001` 场景。

**Architecture:** 在 Compose `live-eval` profile 中新增隔离 FastAPI order-api，使用固定容量 asyncpg pool 和 run-scoped fault token 复现异常路径连接未归还。Live Driver 从 order-api 与 PostgreSQL 收集真实 Runtime 状态，新的场景级 CLS record provider 读取服务实际事件并复用现有 CLS 上传/轮询；Single 与 Multi 共享工具、预算和证据，区别仅为串行主链与 Runtime/Log Investigator 并行 dispatch。

**Tech Stack:** Python 3.10+、FastAPI 0.139.0、uvicorn 0.51.0、asyncpg 0.31.0、httpx、Docker Compose、PostgreSQL 16、腾讯云 CLS MCP、LangGraph、pytest、Ruff、Pyright、OpenSpec。

## Global Constraints

- 不新增 Python 项目依赖；order-api 镜像只安装 `uv.lock` 已有的固定版本 FastAPI、uvicorn、asyncpg。
- Single 与 Multi 必须看到同一公开输入、工具、CLS scope、知识库、模型、全局步骤/模型预算和评分器。
- 不能按 strategy 隐藏工具，不能在 Runtime/CLS 输出中暴露 Oracle、Ground Truth、`primary_cause` 或 `connection_leak_confirmed`。
- order-api 只能操作 `agent_py_live_eval` 数据库和当前安全 run；普通服务、其他 run 和生产数据不得受影响。
- 自动重启只允许作用于 Compose 隔离服务 `live-eval-order-api`，且必须确认没有其他 active run；生产语义始终人工审批。
- 复用现有 terminal envelope、PostgreSQL 结果、Archive checksum、ExecutionCoordinator、checkpoint 和 recovery intent 幂等。
- 不执行全量 pytest；使用风险对应的目标回归。真实 LLM+CLS 3×3 A/B 必须在离线与 Docker 门禁通过后另行确认。
- 不提交 `config/project.json`、`config/user.project.json`、`apps/backend/var/`、Archive、真实凭据或运行日志。

---

### Task 1: 固化 OpenSpec 增量合同

**Files:**
- Create: `openspec/changes/add-order-pool-leak-live-scenario/.openspec.yaml`
- Create: `openspec/changes/add-order-pool-leak-live-scenario/proposal.md`
- Create: `openspec/changes/add-order-pool-leak-live-scenario/design.md`
- Create: `openspec/changes/add-order-pool-leak-live-scenario/tasks.md`
- Create: `openspec/changes/add-order-pool-leak-live-scenario/specs/agentpy-sre-benchmark/spec.md`
- Create: `openspec/changes/add-order-pool-leak-live-scenario/specs/aiops-diagnosis-tasks/spec.md`

**Interfaces:**
- Consumes: 已确认设计 `docs/superpowers/specs/2026-08-20-order-api-pool-leak-multi-agent-design.md`。
- Produces: change id `add-order-pool-leak-live-scenario`；可验证的场景、日志真实性、公平 A/B、恢复与安全 Requirement。

- [ ] **Step 1: 写 OpenSpec change**

`.openspec.yaml` 固定：

```yaml
schema: spec-driven
created: 2026-08-20
```

delta spec 必须包含以下 Requirement 与 Scenario：

```markdown
## ADDED Requirements

### Requirement: Order pool leak Live scenario is cross-source and reproducible
系统 SHALL 使用隔离 order-api 的真实 asyncpg 连接生命周期复现连接池耗尽，并分别从 Runtime 与服务实际日志产生互补证据。

#### Scenario: Runtime alone does not reveal the exception lifecycle
- **WHEN** Runtime Investigator 检查已饱和连接池和 PostgreSQL 会话
- **THEN** 它 SHALL 证明池耗尽与数据库可达，但 SHALL NOT 返回连接泄漏答案标签

#### Scenario: CLS records originate from order-api events
- **WHEN** CLS evidence preparer为 order pool 场景准备日志
- **THEN** 上传记录 SHALL 来自当前 run 的 order-api 事件日志，而不是 evaluator 合成的故障答案

### Requirement: Single and Multi strategy comparison is fair
系统 SHALL 为 Single 与 Multi 暴露完全相同的工具、参数、预算和评分器，只允许调查调度方式不同。

### Requirement: Isolated recovery is scoped and idempotent
系统 SHALL 只在当前 run 独占 order-api 时执行一次服务重启，并独立验证旧连接释放、新 generation 就绪和业务探针恢复。
```

- [ ] **Step 2: 严格验证 OpenSpec**

Run:

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-order-pool-leak-live-scenario --strict
```

Expected: `add-order-pool-leak-live-scenario` valid，退出码 0。

- [ ] **Step 3: 提交规格**

```powershell
git add openspec/changes/add-order-pool-leak-live-scenario
git commit -m "docs: specify order pool leak live scenario"
```

---

### Task 2: 实现可单元测试的隔离 order-api runtime

**Files:**
- Create: `infra/live-eval/order_api.py`
- Create: `infra/live-eval/order-api.Dockerfile`
- Create: `apps/backend/tests/test_live_order_api_service.py`

**Interfaces:**
- Consumes: PostgreSQL DSN、`LIVE_ORDER_API_CONTROL_TOKEN`、固定 `pool_size=3`。
- Produces: `create_app(settings: OrderApiSettings) -> FastAPI`；`OrderApiRuntime`; `/health`、`/internal/runs/start`、`/internal/runs/{run_id}/fault`、`/internal/runs/{run_id}/probe`、`/internal/runs/{run_id}/state`、`/internal/runs/{run_id}/events`、`DELETE /internal/runs/{run_id}`。

- [ ] **Step 1: 写 service RED tests**

使用 fake pool 和固定 clock 断言：

```python
@pytest.mark.asyncio
async def test_fault_path_keeps_checked_out_connection_and_records_real_lifecycle() -> None:
    runtime = OrderApiRuntime(pool=FakePool(max_size=3), generation="gen-1", now=fixed_now)
    await runtime.start_run("run-1", "fault-token")
    await runtime.execute_fault("run-1", "fault-token", "request-1")

    state = await runtime.state("run-1")
    events = await runtime.events("run-1")

    assert state["checkedOut"] == 1
    assert [item["event"] for item in events] == [
        "connection_checkout",
        "order_update_failed",
    ]
    assert "connection_checkin" not in {item["event"] for item in events}
    assert "fault-token" not in str(events)


@pytest.mark.asyncio
async def test_normal_probe_always_returns_connection() -> None:
    runtime = OrderApiRuntime(pool=FakePool(max_size=3), generation="gen-1", now=fixed_now)
    await runtime.start_run("run-1", "fault-token")
    assert await runtime.probe("run-1", timeout_seconds=0.1) is True
    assert runtime.pool.checked_out == 0
    assert [item["event"] for item in await runtime.events("run-1")] == [
        "connection_checkout",
        "connection_checkin",
    ]
```

另测：错误 token 拒绝、另一 active run 拒绝、事件上限、状态输出无 DSN/SQL/异常原文、pool timeout 记录 `pool_acquire_timeout`。

- [ ] **Step 2: 运行 RED**

Run:

```powershell
cd apps/backend
uv run pytest tests/test_live_order_api_service.py -q -p no:cacheprovider
```

Expected: import/file missing FAIL。

- [ ] **Step 3: 实现 runtime 与 FastAPI endpoints**

核心边界必须为：

```python
class PoolBoundary(Protocol):
    async def acquire(self, *, timeout: float) -> asyncpg.Connection: ...
    async def release(self, connection: asyncpg.Connection) -> None: ...
    def get_size(self) -> int: ...
    def get_idle_size(self) -> int: ...


@dataclass(frozen=True, slots=True)
class OrderApiSettings:
    postgres_dsn: str = field(repr=False)
    control_token: str = field(repr=False)
    pool_size: int = 3


class OrderApiRuntime:
    async def start_run(self, run_id: str, fault_token: str) -> None: ...
    async def execute_fault(self, run_id: str, fault_token: str, request_id: str) -> None: ...
    async def probe(self, run_id: str, *, timeout_seconds: float) -> bool: ...
    async def state(self, run_id: str) -> dict[str, object]: ...
    async def events(self, run_id: str) -> tuple[dict[str, str], ...]: ...
    async def clear_run(self, run_id: str) -> None: ...
```

真实 pool 使用 `min_size=0`、`max_size=3`。fault path 在 checkout 后设置 run-scoped
`application_name`，保存 connection 引用并记录 checkout/error，不调用 release；正常 probe 使用
`try/finally` 归还。事件只允许 `run_id`、`request_id`、`event`、`service`、`component`、
`generation`、`timestamp`、`level`。

Dockerfile 使用 root build context `infra/`，固定：

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.139.0 uvicorn==0.51.0 asyncpg==0.31.0
COPY live-eval/order_api.py /opt/live-eval/order_api.py
CMD ["uvicorn", "order_api:create_app", "--factory", "--app-dir", "/opt/live-eval", "--host", "0.0.0.0", "--port", "8082"]
```

- [ ] **Step 4: 运行 GREEN 与静态检查**

```powershell
cd apps/backend
uv run pytest tests/test_live_order_api_service.py -q -p no:cacheprovider
uv run ruff check ../../infra/live-eval/order_api.py tests/test_live_order_api_service.py
uv run pyright ../../infra/live-eval/order_api.py tests/test_live_order_api_service.py
```

Expected: 全部 PASS，0 error。

- [ ] **Step 5: 提交 service**

```powershell
git add infra/live-eval/order_api.py infra/live-eval/order-api.Dockerfile apps/backend/tests/test_live_order_api_service.py
git commit -m "feat: add isolated order pool leak service"
```

---

### Task 3: 将 order-api 接入 Compose 并固定隔离边界

**Files:**
- Modify: `infra/compose.yaml`
- Modify: `apps/backend/tests/test_infra_compose.py`

**Interfaces:**
- Consumes: Task 2 Dockerfile；现有 `postgres` service 与 `live-eval` profile。
- Produces: `live-eval-order-api`，仅监听 `127.0.0.1:18082`，依赖健康 PostgreSQL。

- [ ] **Step 1: 写 Compose RED**

```python
def test_compose_configures_isolated_order_api_for_live_eval_only() -> None:
    compose = _read("compose.yaml")
    assert "live-eval-order-api:" in compose
    assert 'profiles: ["live-eval"]' in compose
    assert '"127.0.0.1:18082:8082"' in compose
    assert "dockerfile: live-eval/order-api.Dockerfile" in compose
    assert "POSTGRES_DB: agent_py_live_eval" in compose
    assert "postgres:\n        condition: service_healthy" in compose
```

- [ ] **Step 2: 运行 RED**

```powershell
cd apps/backend
uv run pytest tests/test_infra_compose.py -q -p no:cacheprovider -k order_api
```

Expected: service missing FAIL。

- [ ] **Step 3: 添加 Compose service**

```yaml
  live-eval-order-api:
    build:
      context: .
      dockerfile: live-eval/order-api.Dockerfile
    profiles: ["live-eval"]
    environment:
      POSTGRES_HOST: postgres
      POSTGRES_PORT: "5432"
      POSTGRES_USER: agent_py
      POSTGRES_PASSWORD: agent_py_dev
      POSTGRES_DB: agent_py_live_eval
      LIVE_ORDER_API_CONTROL_TOKEN: agentpy-live-eval-control
      LIVE_ORDER_API_POOL_SIZE: "3"
    ports:
      - "127.0.0.1:18082:8082"
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8082/health', timeout=1).read()"]
      interval: 2s
      timeout: 2s
      retries: 30
```

- [ ] **Step 4: 验证 Compose**

```powershell
docker compose -f infra/compose.yaml config
cd apps/backend
uv run pytest tests/test_infra_compose.py -q -p no:cacheprovider
```

Expected: Compose valid，目标测试 PASS。

- [ ] **Step 5: 提交 Compose**

```powershell
git add infra/compose.yaml apps/backend/tests/test_infra_compose.py
git commit -m "feat: compose order pool live service"
```

---

### Task 4: 实现 order pool Live Driver、恢复策略与安全审计

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/order_pool_leak.py`
- Create: `apps/backend/tests/test_live_order_pool_contracts.py`

**Interfaces:**
- Consumes: `LiveScenarioDriver`/`LiveRecoveryService` 协议、`PostgresConnectionConfig`、order-api HTTP endpoints。
- Produces: `OrderPoolLiveConfig`、`OrderPoolLeakScenarioDriver`、`OrderPoolRecoveryService`、`OrderPoolLiveRunAudit`、`OrderPoolRuntimeEvidenceMcpClient`、`OrderPoolClsRecordProvider`。

- [ ] **Step 1: 写 Driver/Recovery RED tests**

使用 fake HTTP boundary、fake PostgreSQL observer 和 fake compose restarter：

```python
@pytest.mark.asyncio
async def test_driver_confirms_pool_saturation_without_claiming_the_cause() -> None:
    driver = configured_driver(pool_size=3)
    identity = validate_run_id("order-pool-contract")
    await driver.preflight(identity)
    await driver.baseline(identity)
    observation = await driver.inject(identity)

    assert observation.confirmed is True
    assert observation.check_passed("pool_at_capacity")
    assert observation.check_passed("pool_free_zero")
    assert observation.check_passed("business_probe_timed_out")
    assert observation.check_passed("postgres_reachable")
    assert observation.check_passed("no_lock_wait")
    assert "leak" not in str(observation).casefold()


@pytest.mark.asyncio
async def test_recovery_restarts_only_owned_isolated_instance_once() -> None:
    driver, restarter = configured_recovery_driver(active_run="run-1", generation="gen-1")
    recovery = OrderPoolRecoveryService(driver, restarter)
    record = await recovery.recover(
        identity=validate_run_id("run-1"),
        diagnostic_artifact=passing_order_pool_artifact(),
        observation=confirmed_order_pool_observation(),
    )

    assert record.action == "restart_live_eval_order_api"
    assert record.target_ref == "current_run_order_api_instance"
    assert record.authorized is True
    assert record.executed is True
    assert restarter.calls == [("live-eval-order-api",)]
```

另测：错误 component/mechanism、另一 active run、generation 变化前、数据库不可达、非隔离 compose
路径均拒绝；config repr 不含 token/password；cleanup 两次幂等；audit 只返回残留数量。

- [ ] **Step 2: 运行 RED**

```powershell
cd apps/backend
uv run pytest tests/test_live_order_pool_contracts.py -q -p no:cacheprovider
```

Expected: module missing FAIL。

- [ ] **Step 3: 实现边界和状态机**

定义：

```python
class OrderApiControlBoundary(Protocol):
    async def health(self) -> Mapping[str, object]: ...
    async def start_run(self, identity: LiveRunIdentity, fault_token: str) -> None: ...
    async def inject_fault(self, identity: LiveRunIdentity, fault_token: str, request_id: str) -> None: ...
    async def probe(self, identity: LiveRunIdentity) -> bool: ...
    async def state(self, identity: LiveRunIdentity) -> Mapping[str, object]: ...
    async def events(self, identity: LiveRunIdentity) -> Sequence[Mapping[str, str]]: ...
    async def clear(self, identity: LiveRunIdentity) -> None: ...


class ComposeRestartBoundary(Protocol):
    async def restart(self, service_name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class OrderPoolLiveConfig:
    base_url: str = "http://127.0.0.1:18082"
    control_token: str = field(default="agentpy-live-eval-control", repr=False)
    pool_size: int = 3
    probe_timeout_seconds: float = 0.5
    compose_file: Path = REPOSITORY_ROOT / "infra" / "compose.yaml"
    service_name: str = "live-eval-order-api"
```

真实 restarter 必须使用 `asyncio.create_subprocess_exec` 的固定参数，不使用 shell 或用户输入：

```python
("docker", "compose", "-f", str(config.compose_file), "restart", config.service_name)
```

Driver 注入恰好 `pool_size` 个 fault request，随后执行 timeout probe，并分别查询 pool state、
`pg_stat_activity`、lock/deadlock 信号。Runtime observation 只投影布尔检查，不包含 fault token、PID、
原始 SQL、异常文本或根因标签。

- [ ] **Step 4: 实现恢复验证与审计**

`verify()` 必须检查：旧 generation 不再存在、旧 application_name 会话为零、新 generation 就绪、
新业务 probe 成功、PostgreSQL 健康、其他 application_name 会话未被终止。`audit()` 必须检查 active
run 为零、测试订单为零、旧 generation 会话为零。Recovery 只在 decision component/mechanism 与
当前观察一致且当前 run 独占服务时授权。

- [ ] **Step 5: 运行 GREEN 与静态检查**

```powershell
cd apps/backend
uv run pytest tests/test_live_order_pool_contracts.py -q -p no:cacheprovider
uv run ruff check src/super_ai/evaluation/live/order_pool_leak.py tests/test_live_order_pool_contracts.py
uv run pyright src/super_ai/evaluation/live/order_pool_leak.py tests/test_live_order_pool_contracts.py
```

Expected: 全部 PASS，0 error。

- [ ] **Step 6: 提交 Driver/Recovery**

```powershell
git add apps/backend/src/super_ai/evaluation/live/order_pool_leak.py apps/backend/tests/test_live_order_pool_contracts.py
git commit -m "feat: drive and recover order pool leak"
```

---

### Task 5: 让 CLS 使用 order-api 实际事件而非合成模板

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/cls_evidence.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/registry.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/evidence_client.py`
- Modify: `apps/backend/tests/test_live_cls_evidence.py`
- Modify: `apps/backend/tests/test_live_benchmark_cli.py`

**Interfaces:**
- Consumes: `OrderPoolClsRecordProvider.records(identity, scenario, observation, now)`。
- Produces: `LiveClsRecordProvider` Protocol；`LiveClsEvidencePreparer(record_provider=...)`；`LiveScenarioComponents.cls_record_provider`；新 CLS evidence id `cls-order-connection-lifecycle`。

- [ ] **Step 1: 写 actual-record RED**

```python
@pytest.mark.asyncio
async def test_order_pool_cls_preparer_uploads_provider_records() -> None:
    provider = RecordingOrderPoolProvider(
        events=(
            {"event": "connection_checkout", "request_id": "req-1"},
            {"event": "order_update_failed", "request_id": "req-1"},
            {"event": "pool_acquire_timeout", "request_id": "probe-1"},
        )
    )
    preparer = build_preparer(record_provider=provider)
    context = await preparer.prepare(
        identity=validate_run_id("run-1"),
        scenario=order_pool_scenario(),
        observation=confirmed_order_pool_observation(),
    )

    assert provider.calls == ["run-1"]
    assert uploader.records[0]["event"] == "connection_checkout"
    assert context.readiness is not None
    assert context.readiness.expected_log_count == 3
```

另测：旧四场景继续调用 `build_live_cls_records`；provider 返回非法 key、错误 run_id、Oracle key、
空记录或超限记录时 `LiveInfrastructureError("cls_records_invalid")`；`_cls_evidence_id` 对新场景
返回中性 evidence id。

- [ ] **Step 2: 运行 RED**

```powershell
cd apps/backend
uv run pytest tests/test_live_cls_evidence.py tests/test_live_benchmark_cli.py -q -p no:cacheprovider -k "record_provider or order_pool"
```

Expected: Protocol/constructor/registry missing FAIL。

- [ ] **Step 3: 实现可注入 record provider**

```python
class LiveClsRecordProvider(Protocol):
    async def records(
        self,
        *,
        identity: LiveRunIdentity,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
        now: datetime,
    ) -> Sequence[Mapping[str, str]]: ...
```

`LiveClsEvidencePreparer.prepare()` 在 provider 存在时 await 实际记录，否则保持当前模板。所有 provider
记录必须经 `_validate_safe_cls_records()`：只允许固定字段与 event enum，强制当前 run/scenario/incident
scope，禁止 `oracle`、`ground_truth`、`primary_cause`、token、password、DSN、SQL 等 key。

`_run_live_command()` 先 resolve scenario components，再把 `components.cls_record_provider` 传给
`build_live_evidence_runtime()`。旧 registry factory 不传 provider，行为不变。

- [ ] **Step 4: 运行 GREEN 和相关回归**

```powershell
cd apps/backend
uv run pytest tests/test_live_cls_evidence.py tests/test_live_evidence_client.py tests/test_live_benchmark_cli.py tests/test_live_cli_contract.py -q -p no:cacheprovider
uv run ruff check src/super_ai/evaluation/live/cls_evidence.py src/super_ai/evaluation/live/registry.py src/super_ai/evaluation/live/cli.py src/super_ai/evaluation/live/evidence_client.py
uv run pyright src/super_ai/evaluation/live/cls_evidence.py src/super_ai/evaluation/live/registry.py src/super_ai/evaluation/live/cli.py src/super_ai/evaluation/live/evidence_client.py
```

Expected: 全部 PASS，0 error。

- [ ] **Step 5: 提交 CLS provider**

```powershell
git add apps/backend/src/super_ai/evaluation/live/cls_evidence.py apps/backend/src/super_ai/evaluation/live/registry.py apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/src/super_ai/evaluation/live/evidence_client.py apps/backend/tests/test_live_cls_evidence.py apps/backend/tests/test_live_benchmark_cli.py
git commit -m "feat: upload actual order api live events"
```

---

### Task 6: 暴露公平的 Runtime/CLS 工具并支持跨源路由

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/order_pool_leak.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/aiops/investigation.py`
- Modify: `apps/backend/src/super_ai/aiops/causal_intents.py`
- Modify: `apps/backend/tests/test_live_order_pool_contracts.py`
- Modify: `apps/backend/tests/test_aiops_investigation_router.py`
- Modify: `apps/backend/tests/test_aiops_causal_intents.py`
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`

**Interfaces:**
- Consumes: Task 4 的真实 Runtime checks；CLS 官方 `SearchLog`。
- Produces: read-only tools `InspectOrderPoolState`、`InspectOrderDatabaseSessions`、`VerifyOrderDatabaseReachability`；trusted server `order-pool-live`；Single/Multi 等权计划。

- [ ] **Step 1: 写工具与公平路由 RED**

```python
@pytest.mark.asyncio
async def test_order_pool_runtime_tools_are_partial_and_answer_isolated() -> None:
    client = OrderPoolRuntimeEvidenceMcpClient(confirmed_order_pool_observation())
    pool = await client.call_tool("InspectOrderPoolState", {})
    sessions = await client.call_tool("InspectOrderDatabaseSessions", {})

    assert pool["poolAtCapacity"] is True
    assert sessions["databaseReachable"] is True
    serialized = f"{pool}{sessions}".casefold()
    assert "connection_leak" not in serialized
    assert "primary_cause" not in serialized


def test_single_and_multi_receive_the_same_order_pool_tool_catalog() -> None:
    tools = order_pool_discovered_tools(with_cls=True)
    single = build_strategy_fixture("single", tools)
    multi = build_strategy_fixture("multi", tools)
    assert single.available_tool_names == multi.available_tool_names
    assert {"InspectOrderPoolState", "InspectOrderDatabaseSessions", "SearchLog"} <= set(single.available_tool_names)
```

另测：forced Single 实际 `single_agent` 且计划包含 Runtime + SearchLog；forced Multi 实际
`multi_agent` 且 selected investigators 为 runtime/log；两个策略的 trusted arguments 和 budget 相等；
未知 server 或写工具不能成为 Investigator capability。

- [ ] **Step 2: 运行 RED**

```powershell
cd apps/backend
uv run pytest tests/test_live_order_pool_contracts.py tests/test_aiops_investigation_router.py tests/test_aiops_causal_intents.py tests/test_live_diagnostic_adapter.py -q -p no:cacheprovider -k order_pool
```

Expected: tool/capability/generic plan missing FAIL。

- [ ] **Step 3: 实现工具合同和 generic plan**

工具必须使用 server name `order-pool-live`，参数 schema `additionalProperties: false`，输出分别为：

```python
{"poolAtCapacity": True, "freeConnections": 0, "waiterObserved": True, "benchmarkEvidenceId": "order-pool-saturated"}
{"databaseReachable": True, "runScopedSessionsPresent": True, "lockWaitObserved": False, "benchmarkEvidenceId": "order-db-sessions"}
{"databaseReachable": True, "businessProbeTimedOut": True, "benchmarkEvidenceId": "order-pool-acquire-timeout"}
```

`build_generic_live_plan()` 为上述工具提供公开 hypothesis 与 causalIntent。把三个工具加入
`TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES` 的 Runtime allowlist，把 `order-pool-live` 加入可信 Runtime
server。`SearchLog` 允许 `trigger` causal intent，但默认 generic SearchLog step 仍为 `context`，避免改变
旧场景计划；当连接生命周期是缺失 trigger 时 coverage repair 才能选择它。

- [ ] **Step 4: 运行 GREEN 与跨源回归**

```powershell
cd apps/backend
uv run pytest tests/test_live_order_pool_contracts.py tests/test_aiops_investigation_router.py tests/test_aiops_causal_intents.py tests/test_live_diagnostic_adapter.py tests/test_aiops_multi_agent_runtime.py tests/test_aiops_v4_workflow.py -q -p no:cacheprovider
uv run ruff check src/super_ai/aiops src/super_ai/evaluation/live/order_pool_leak.py
uv run pyright src/super_ai/aiops src/super_ai/evaluation/live/order_pool_leak.py
```

Expected: 全部 PASS，0 error；旧四场景计划不回退。

- [ ] **Step 5: 提交公平路由**

```powershell
git add apps/backend/src/super_ai/evaluation/live/order_pool_leak.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/aiops/investigation.py apps/backend/src/super_ai/aiops/causal_intents.py apps/backend/tests/test_live_order_pool_contracts.py apps/backend/tests/test_aiops_investigation_router.py apps/backend/tests/test_aiops_causal_intents.py apps/backend/tests/test_live_diagnostic_adapter.py
git commit -m "feat: route order pool runtime and log evidence"
```

---

### Task 7: 添加场景、Oracle、语义与评分合同

**Files:**
- Create: `benchmarks/agentpy/live/APY-LIVE-ORDER-POOL-LEAK-001/scenario.yaml`
- Create: `benchmarks/agentpy/live/APY-LIVE-ORDER-POOL-LEAK-001/ground_truth.yaml`
- Create: `benchmarks/agentpy/live/APY-LIVE-ORDER-POOL-LEAK-001/provenance.yaml`
- Modify: `apps/backend/src/super_ai/evaluation/live/diagnostics.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/evidence_client.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/scoring.py`
- Modify: `apps/backend/tests/test_live_evaluation_scenarios.py`
- Modify: `apps/backend/tests/test_live_evaluation_scoring.py`
- Modify: `apps/backend/tests/test_live_semantic_scoring.py`

**Interfaces:**
- Consumes: public tools/evidence ids from Tasks 5-6。
- Produces: driver `order_pool_leak`；canonical cause `order-api / exception_path_connection_not_released`；100-point scoring and citation sources。

- [ ] **Step 1: 写 fixture/scoring RED**

```python
def test_order_pool_scenario_requires_runtime_and_cls_lifecycle_evidence() -> None:
    scenario = load_live_scenario(ORDER_POOL_SCENARIO)
    oracle = load_live_oracle(ORDER_POOL_SCENARIO)
    assert scenario.driver == "order_pool_leak"
    assert oracle.primary_cause.component == "order-api"
    assert oracle.primary_cause.mechanism == "exception_path_connection_not_released"
    assert required_citation_sources(scenario.id) == {"InspectOrderPoolState", "SearchLog"}
```

另测：完整同义表达得 root cause 20；只有 pool saturation 不得获得 trigger 分；只有 CLS lifecycle 不得
满足 Runtime required evidence；恢复/cleanup 失败继续触发 hard gate。

- [ ] **Step 2: 运行 RED**

```powershell
cd apps/backend
uv run pytest tests/test_live_evaluation_scenarios.py tests/test_live_evaluation_scoring.py tests/test_live_semantic_scoring.py -q -p no:cacheprovider -k order_pool
```

Expected: scenario missing FAIL。

- [ ] **Step 3: 写公开场景和私有 Oracle**

公开 hypotheses 固定：

```yaml
hypotheses:
  - id: order_connection_lifecycle_leak
  - id: order_legitimate_concurrency_pressure
  - id: order_database_connectivity_failure
  - id: order_slow_query
  - id: order_database_lock_contention
```

Oracle required evidence：

```yaml
required_evidence:
  - id: order-pool-saturation-and-reachability
    alternatives:
      - [order-pool-saturated, order-db-sessions]
  - id: order-request-timeout-after-saturation
    alternatives:
      - [order-pool-acquire-timeout]
cls_required_evidence:
  - id: order-connection-lifecycle-events
    alternatives:
      - [cls-order-connection-lifecycle]
```

required rule-outs 为四个非主因 hypothesis；recovery expectation 为 `executed_recovery`。semantic concepts
覆盖 exception path、checkout、missing release、pool saturation、waiter、timeout、causal link 四个里程碑。
provenance 标记 AgentPy 原创 fixture，引用 PostgreSQL monitoring、asyncpg pool 与现有项目知识卡，不复制
第三方文本。

- [ ] **Step 4: 接入 decision vocabulary、CLS id 和 citation sources**

`_decision_vocabulary()` 添加五个 hypothesis 映射；`_cls_evidence_id()` 添加新场景；
`_CITATION_SOURCES` 添加 `InspectOrderPoolState + SearchLog`。不得在公开 input 中注入 Oracle trigger。

- [ ] **Step 5: 运行 GREEN**

```powershell
cd apps/backend
uv run pytest tests/test_live_evaluation_scenarios.py tests/test_live_evaluation_scoring.py tests/test_live_semantic_scoring.py tests/test_evaluation_scenarios.py -q -p no:cacheprovider
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交场景合同**

```powershell
git add benchmarks/agentpy/live/APY-LIVE-ORDER-POOL-LEAK-001 apps/backend/src/super_ai/evaluation/live/diagnostics.py apps/backend/src/super_ai/evaluation/live/evidence_client.py apps/backend/src/super_ai/evaluation/live/scoring.py apps/backend/tests/test_live_evaluation_scenarios.py apps/backend/tests/test_live_evaluation_scoring.py apps/backend/tests/test_live_semantic_scoring.py
git commit -m "feat: score order pool leak live scenario"
```

---

### Task 8: 注册 CLI runtime、恢复 allowlist 与幂等执行

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/scoring.py`
- Modify: `apps/backend/tests/test_live_benchmark_cli.py`
- Modify: `apps/backend/tests/test_live_benchmark_runner.py`
- Modify: `apps/backend/tests/test_aiops_checkpointing.py`

**Interfaces:**
- Consumes: Task 4 components；现有 `build_live_recovery_coordinator()`。
- Produces: registry entry `APY-LIVE-ORDER-POOL-LEAK-001`；env loader；scoped recovery action allowlist；重复 run/recovery 安全恢复。

- [ ] **Step 1: 写 registry/idempotency RED**

```python
def test_registry_resolves_order_pool_runtime() -> None:
    components = build_live_scenario_registry().resolve("APY-LIVE-ORDER-POOL-LEAK-001")
    assert isinstance(components.driver, OrderPoolLeakScenarioDriver)
    assert isinstance(components.recovery, OrderPoolRecoveryService)
    assert components.cls_record_provider is not None


@pytest.mark.asyncio
async def test_replayed_order_pool_recovery_restarts_once() -> None:
    first = await coordinator.execute(prepared_restart_intent())
    second = await coordinator.execute(prepared_restart_intent())
    assert first == second
    assert restarter.call_count == 1
```

- [ ] **Step 2: 运行 RED**

```powershell
cd apps/backend
uv run pytest tests/test_live_benchmark_cli.py tests/test_live_benchmark_runner.py tests/test_aiops_checkpointing.py -q -p no:cacheprovider -k order_pool
```

Expected: registry/action missing FAIL。

- [ ] **Step 3: 注册 components 与环境配置**

新增 `_order_pool_config_from_environment()`，只读取 `LIVE_ORDER_API_URL`、
`LIVE_ORDER_API_CONTROL_TOKEN`、既有 Live PostgreSQL 环境变量；database 固定
`agent_py_live_eval`，compose path 固定仓库 `infra/compose.yaml`。Factory 构建一个 driver，并把同一
实例传给 recovery、Runtime evidence factory 和 CLS record provider。

`_executed_action_is_scoped()` 只新增：

```python
"restart_live_eval_order_api": {"current_run_order_api_instance"}
```

不得把任意 restart 工具加入 Agent tool allowlist。

- [ ] **Step 4: 运行 GREEN 与恢复回归**

```powershell
cd apps/backend
uv run pytest tests/test_live_benchmark_cli.py tests/test_live_benchmark_runner.py tests/test_aiops_checkpointing.py tests/test_aiops_network_resume.py tests/test_live_evaluation_scoring.py -q -p no:cacheprovider
```

Expected: 全部 PASS，重复恢复只执行一次。

- [ ] **Step 5: 提交 CLI/幂等接入**

```powershell
git add apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/src/super_ai/evaluation/live/scoring.py apps/backend/tests/test_live_benchmark_cli.py apps/backend/tests/test_live_benchmark_runner.py apps/backend/tests/test_aiops_checkpointing.py
git commit -m "feat: register idempotent order pool live runtime"
```

---

### Task 9: 补答案隔离、跨 run 与安全失败路径

**Files:**
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`
- Modify: `apps/backend/tests/test_live_cls_evidence.py`
- Modify: `apps/backend/tests/test_live_order_pool_contracts.py`
- Modify: `apps/backend/tests/test_knowledge_candidate_safety.py`
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`

**Interfaces:**
- Consumes: 新场景公开 input、Runtime tool outputs、实际 CLS records。
- Produces: 0 Oracle 泄漏、0 跨 run Evidence、0 未授权 restart 的回归门禁。

- [ ] **Step 1: 写安全 RED**

覆盖：

```python
def test_order_pool_agent_input_and_tools_do_not_expose_oracle() -> None:
    serialized = json.dumps(
        {"input": build_live_diagnostic_input(order_pool_scenario()), "tools": order_pool_safe_outputs()},
        ensure_ascii=False,
    ).casefold()
    for forbidden in ("ground_truth", "primary_cause", "connection_leak_confirmed", "fault_token"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_order_pool_cls_provider_rejects_cross_run_event() -> None:
    provider = provider_with_event(run_id="other-run")
    with pytest.raises(LiveInfrastructureError, match="cls_records_invalid"):
        await prepare_order_pool_cls(provider, expected_run_id="run-1")
```

另测：`../` scenario traversal、嵌套 `oracle` key、`ReadGroundTruth`、Agent 尝试 `RestartOrderApi`、
另一 active run、cleanup failure、generation 未改变、数据库 session 范围不匹配。

- [ ] **Step 2: 运行安全测试并修复最小缺口**

```powershell
cd apps/backend
uv run pytest tests/test_live_diagnostic_adapter.py tests/test_live_cls_evidence.py tests/test_live_order_pool_contracts.py tests/test_knowledge_candidate_safety.py tests/test_evaluation_scenarios.py -q -p no:cacheprovider -k "order_pool or ground_truth or traversal or oracle"
```

Expected: 全部 PASS；不得通过弱化断言实现 GREEN。

- [ ] **Step 3: 提交安全回归**

```powershell
git add apps/backend/tests/test_live_diagnostic_adapter.py apps/backend/tests/test_live_cls_evidence.py apps/backend/tests/test_live_order_pool_contracts.py apps/backend/tests/test_knowledge_candidate_safety.py apps/backend/tests/test_evaluation_scenarios.py
git commit -m "test: harden order pool live isolation"
```

---

### Task 10: 执行真实 Docker 注入、恢复和 Cleanup 合同

**Files:**
- Create: `apps/backend/tests/test_live_order_pool_docker.py`
- Modify: `docs/knowledge-candidates/postgres-pool-exhaustion.md`

**Interfaces:**
- Consumes: 本机 Docker、`postgres`、`live-eval-order-api`。
- Produces: 无 LLM/CLS 的真实 asyncpg 泄漏与重启闭环证据；知识卡 `docker_validation` 状态。

- [ ] **Step 1: 写显式 opt-in Docker test**

```python
pytestmark = pytest.mark.live_docker


@pytest.mark.asyncio
async def test_real_order_pool_leak_recovery_and_idempotent_cleanup() -> None:
    identity = validate_run_id("docker-order-pool-contract")
    driver = real_order_pool_driver()
    try:
        await driver.preflight(identity)
        await driver.baseline(identity)
        observation = await driver.inject(identity)
        assert observation.confirmed
        recovery = await OrderPoolRecoveryService(driver, real_compose_restarter()).recover(
            identity=identity,
            diagnostic_artifact=passing_order_pool_artifact(),
            observation=observation,
        )
        assert recovery.authorized and recovery.executed
        assert (await driver.verify(identity)).passed
    finally:
        await driver.cleanup(identity)
        await driver.cleanup(identity)
    assert (await driver.audit(identity)).clean
```

- [ ] **Step 2: 启动隔离服务并运行 Docker test**

```powershell
docker compose -f infra/compose.yaml --profile live-eval up -d --build postgres live-eval-order-api
cd apps/backend
uv run pytest tests/test_live_order_pool_docker.py -q -p no:cacheprovider -m live_docker
```

Expected: 真实 checkout 累积、pool timeout、scoped restart、generation 更新、业务恢复、双 cleanup
全部 PASS。失败时必须先运行 cleanup/audit，不能继续真实 A/B。

- [ ] **Step 3: 更新知识卡验证状态**

只有 Step 2 通过后，把 `postgres-pool-exhaustion.md` 的验证状态改为：

```text
docker_validation: passed-order-api-pool-leak-2026-08-20
```

不得把本次单场景验证描述成覆盖所有连接池实现。

- [ ] **Step 4: 提交 Docker 合同**

```powershell
git add apps/backend/tests/test_live_order_pool_docker.py docs/knowledge-candidates/postgres-pool-exhaustion.md
git commit -m "test: validate order pool leak docker recovery"
```

---

### Task 11: 运行目标回归、静态检查和文档验收

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `openspec/changes/add-order-pool-leak-live-scenario/tasks.md`

**Interfaces:**
- Consumes: Tasks 1-10 的实现和 Docker 证据。
- Produces: 离线/Live 实现记录；未执行付费 A/B 的明确边界。

- [ ] **Step 1: 运行目标后端回归**

```powershell
cd apps/backend
uv run pytest tests/test_live_order_api_service.py tests/test_live_order_pool_contracts.py tests/test_live_order_pool_docker.py tests/test_live_cls_evidence.py tests/test_live_evidence_client.py tests/test_live_benchmark_cli.py tests/test_live_benchmark_runner.py tests/test_live_diagnostic_adapter.py tests/test_live_evaluation_scenarios.py tests/test_live_evaluation_scoring.py tests/test_live_semantic_scoring.py tests/test_aiops_investigation_router.py tests/test_aiops_causal_intents.py tests/test_aiops_multi_agent_runtime.py tests/test_aiops_checkpointing.py tests/test_aiops_network_resume.py tests/test_evaluation_scenarios.py tests/test_knowledge_candidate_safety.py tests/test_infra_compose.py -q -p no:cacheprovider
```

Expected: 全部 PASS；不运行全量 pytest。

- [ ] **Step 2: 运行 Ruff、Pyright、Compose 和 OpenSpec**

```powershell
cd apps/backend
uv run ruff check src/super_ai/aiops src/super_ai/evaluation tests/test_live_order_api_service.py tests/test_live_order_pool_contracts.py tests/test_live_order_pool_docker.py
uv run pyright src/super_ai/aiops src/super_ai/evaluation tests/test_live_order_api_service.py tests/test_live_order_pool_contracts.py tests/test_live_order_pool_docker.py
cd ../..
docker compose -f infra/compose.yaml config
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-order-pool-leak-live-scenario --strict
```

Expected: 全部退出 0。

- [ ] **Step 3: 更新文档和 OpenSpec tasks**

`agentpy-domainbench.md` 只记录：场景机制、实际服务日志链、Docker run id、恢复/cleanup 结果、目标测试
命令和 Git SHA。明确写“尚未执行真实 LLM+CLS A/B，不能宣称 Multi 能力增益”。OpenSpec tasks 只勾选
已有代码和测试证据支持的条目。

- [ ] **Step 4: 提交实现验收记录**

```powershell
git add docs/aiops/agentpy-domainbench.md openspec/changes/add-order-pool-leak-live-scenario/tasks.md
git commit -m "docs: record order pool live implementation acceptance"
```

---

### Task 12: 经用户确认后执行真实 Single/Multi A/B 发布门禁

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `openspec/changes/add-order-pool-leak-live-scenario/tasks.md`

**Interfaces:**
- Consumes: 已通过 Task 11 的同一 Git SHA、active/indexed 30-card KB、真实 Qwen/CLS 配置。
- Produces: 3×3 terminal envelopes、PostgreSQL result rows、Archive checksums、正式 A/B comparison。

- [ ] **Step 1: 在付费调用前请求用户确认**

报告 Docker/离线门禁、预计 6 个 Live run、当前模型和 CLS 配置，但不打印凭据。没有用户明确确认时停止。

- [ ] **Step 2: 审计知识库和环境**

```powershell
cd apps/backend
uv run python scripts/audit_knowledge_index_scope.py --owner-user-id <VERIFIED_OWNER> --knowledge-base-id <ACTIVE_30_CARD_KB> --config ../../config/project.json
docker compose -f ../../infra/compose.yaml ps
```

Expected: 30 documents、180 chunks、0 scope mismatch；PostgreSQL/order-api healthy。

- [ ] **Step 3: 顺序执行 3×3 A/B**

同一 campaign，唯一 run id；严格串行，单条失败立即 verify/cleanup 并停止：

```powershell
uv run python scripts/run_live_benchmark.py run --scenario APY-LIVE-ORDER-POOL-LEAK-001 --run-id <SINGLE_RUN_ID> --owner-user-id <VERIFIED_OWNER> --knowledge-base-id <ACTIVE_30_CARD_KB> --evidence-source cls --strategy single --campaign-id <CAMPAIGN_ID> --config ../../config/project.json
uv run python scripts/run_live_benchmark.py run --scenario APY-LIVE-ORDER-POOL-LEAK-001 --run-id <MULTI_RUN_ID> --owner-user-id <VERIFIED_OWNER> --knowledge-base-id <ACTIVE_30_CARD_KB> --evidence-source cls --strategy multi --campaign-id <CAMPAIGN_ID> --config ../../config/project.json
```

每条必须 `VALID_PASS`、verification/cleanup true；Multi 必须实际 `multi_agent`，Single 必须实际
`single_agent`。不得挑选性删除或覆盖失败 run。

- [ ] **Step 4: 生成正式比较与发布判定**

使用持久化 terminal envelope 调用 `compare_investigation_strategies()`。门禁：

```text
quality non-regression = true
security gate = true
Multi P95 <= Single P95 * 1.5
maximum extra model calls <= 2
maximum duplicate evidence <= 10%
Evidence Recall gain >= 10pp OR Root Cause Top-1 gain >= 5pp
```

若能力增益不存在，结果必须为 `benchmark_only/capability_gain_missing`；不得降低门槛或修改场景追逐
结果。若全部通过，只能标记 `eligible_for_default_review`，生产是否默认启用仍由用户决定。

- [ ] **Step 5: 持久化验收文档并提交**

记录 6 个 run id、checksum、score、Top-1、Recall、P50/P95、模型调用、fallback、重复 Evidence、
安全门禁和 eligibility，不提交 runtime Artifact。

```powershell
git add docs/aiops/agentpy-domainbench.md openspec/changes/add-order-pool-leak-live-scenario/tasks.md
git commit -m "test: evaluate order pool multi agent routing"
```

## Final Acceptance Checklist

- [ ] OpenSpec strict validation PASS。
- [ ] order-api 使用真实 asyncpg pool，故障只作用于当前 run。
- [ ] CLS 上传来自 order-api 实际事件，非 evaluator 合成答案。
- [ ] Runtime 与 CLS 单独不充分，组合后可形成完整因果链。
- [ ] Single/Multi 工具、参数、预算和评分完全对等。
- [ ] 隔离恢复只重启当前独占 order-api，一次且可审计。
- [ ] 旧连接释放、新 generation 就绪、业务 probe 和 cleanup 全部验证。
- [ ] 0 Ground Truth 泄漏、0 跨 run Evidence、0 未授权写工具、0 重复恢复。
- [ ] 目标 pytest、Ruff、Pyright、Compose、OpenSpec 全部通过；未运行全量 pytest。
- [ ] 未经用户确认不执行付费 3×3 A/B；未达能力门槛不默认启用 Multi。
