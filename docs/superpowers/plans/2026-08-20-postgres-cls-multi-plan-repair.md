# PostgreSQL CLS Multi Plan Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Implementation remains with the primary agent; one subagent may review this plan only.

**Goal:** Make the PostgreSQL Lock Live plan expose trusted Runtime and CLS Log domains so Benchmark `strategy=multi` can execute a real Multi-Agent route without Oracle access.

**Architecture:** Reuse the existing tool capability registry, Live plan builder, trusted argument binding and Strategy Router. Register the already project-owned `docker-live-postgres` MCP server as a trusted Runtime source, then merge one bounded `SearchLog` step into CLS-capable Live plans before contract normalization; keep all existing hard gates and fail-closed behavior.

**Tech Stack:** Python 3.10+, pytest/pytest-asyncio, LangGraph 1.2.8+, existing MCP tool contracts, Ruff, Pyright.

## Global Constraints

- Do not read or pass `ground_truth.yaml`, Oracle fields or evaluator-private labels into planning or routing.
- Do not change Router thresholds, Validator rules, scoring, recovery permissions or production strategy forcing.
- Do not add dependencies or trust arbitrary MCP server names.
- Do not call real LLM, CLS, Docker or Live fixtures during target verification.
- Do not run the full pytest suite.
- Do not commit credentials, user configuration, `var/` or Benchmark Archive artifacts.

---

### Task 1: Trust the project-owned PostgreSQL Live Runtime server

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/investigation.py:310-325`
- Test: `apps/backend/tests/test_aiops_investigation_router.py`

**Interfaces:**
- Consumes: `build_investigator_capabilities` and `TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES`.
- Produces: Runtime capability mapping for discovered tools whose `server_name` is exactly `docker-live-postgres`.

- [ ] **Step 1: Write the failing capability test**

Add the production registry import and a test using the real server name:

```python
from super_ai.aiops.investigation import TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES


def test_project_owned_postgres_live_server_is_a_trusted_runtime_source() -> None:
    capabilities = build_investigator_capabilities(
        discovered_tools=(
            _tool("InspectPostgresSessions", server="docker-live-postgres"),
            _tool("SearchLog", server="cls"),
        ),
        trusted_tool_capabilities=TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
        tool_policies={},
        retrieval_available=True,
        cls_available=True,
    )

    assert capabilities["runtime"].allowed_tools == frozenset(
        {"InspectPostgresSessions"}
    )
    assert capabilities["log"].allowed_tools == frozenset({"SearchLog"})
```

Keep the existing unknown-server rejection test unchanged; together the two tests prove the allowlist does not become open-ended.

- [ ] **Step 2: Run the test and verify RED**

Run from `apps/backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_aiops_investigation_router.py::test_project_owned_postgres_live_server_is_a_trusted_runtime_source -q
```

Expected: FAIL because the Runtime capability is unavailable or has an empty tool set.

- [ ] **Step 3: Add the exact trusted server name**

Add one item to `_RUNTIME_SERVER_NAMES`:

```python
_RUNTIME_SERVER_NAMES = frozenset(
    {
        "default",
        "docker-live-postgres",
        "live-eval-local",
        "nginx-timeout-live",
        "postgres-deadlock-live",
        "redis-maxclients-live",
        "snapshot",
    }
)
```

- [ ] **Step 4: Verify GREEN and the fail-closed neighbor**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_aiops_investigation_router.py::test_project_owned_postgres_live_server_is_a_trusted_runtime_source tests/test_aiops_investigation_router.py::test_capability_registry_rejects_a_trusted_name_from_an_untrusted_server -q
```

Expected: 2 passed.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- apps/backend/src/super_ai/aiops/investigation.py apps/backend/tests/test_aiops_investigation_router.py
git commit -m "fix: trust postgres live runtime source"
```

### Task 2: Merge one bounded CLS step into non-empty Live plans

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py:280-330,5420-5515`
- Test: `apps/backend/tests/test_live_diagnostic_adapter.py`

**Interfaces:**
- Produces: `merge_live_log_plan_step(plan: Sequence[JsonDict], *, search_step: JsonDict | None, maximum_steps: int = 4) -> list[JsonDict]`.
- Consumes: `_generic_search_log_step(query)` only when discovered tools include `SearchLog`; existing trusted arguments and MCP schema normalization remain authoritative.

- [ ] **Step 1: Write failing append, deduplication, disabled and bound tests**

Import the new helper and add focused tests:

```python
from super_ai.aiops.diagnostics import merge_live_log_plan_step


def _search_step() -> dict[str, object]:
    return {"id": "search-cls-logs", "tool": "SearchLog", "arguments": {}}


def test_live_log_step_is_merged_into_a_non_empty_runtime_plan() -> None:
    runtime = [{"id": "runtime-1", "tool": "InspectPostgresSessions"}]

    merged = merge_live_log_plan_step(runtime, search_step=_search_step())

    assert [step["tool"] for step in merged] == [
        "InspectPostgresSessions",
        "SearchLog",
    ]
    assert runtime == [{"id": "runtime-1", "tool": "InspectPostgresSessions"}]


def test_live_log_step_is_not_duplicated_or_added_without_cls() -> None:
    existing = [_search_step()]

    assert merge_live_log_plan_step(existing, search_step=_search_step()) == existing
    assert merge_live_log_plan_step(existing, search_step=None) == existing


def test_live_log_step_reserves_one_slot_in_the_bounded_initial_plan() -> None:
    runtime = [
        {"id": f"runtime-{index}", "tool": "InspectPostgresSessions"}
        for index in range(4)
    ]

    merged = merge_live_log_plan_step(runtime, search_step=_search_step(), maximum_steps=4)

    assert len(merged) == 4
    assert merged == runtime


def test_live_log_step_rejects_an_invalid_plan_bound() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        merge_live_log_plan_step([], search_step=_search_step(), maximum_steps=0)
```

- [ ] **Step 2: Run the helper tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_live_diagnostic_adapter.py -q -k "live_log_step"
```

Expected: collection ERROR because `merge_live_log_plan_step` does not exist.

- [ ] **Step 3: Implement the pure bounded merge helper**

Place it beside `build_generic_live_plan`:

```python
def merge_live_log_plan_step(
    plan: Sequence[JsonDict],
    *,
    search_step: JsonDict | None,
    maximum_steps: int = 4,
) -> list[JsonDict]:
    if maximum_steps <= 0:
        raise ValueError("Live diagnostic plan limit must be positive.")
    copied = [dict(step) for step in plan]
    if search_step is None or any(
        step.get("tool") in {"SearchLog", "SearchLogs"} for step in copied
    ):
        return copied
    if len(copied) >= maximum_steps:
        return copied
    return [*copied, dict(search_step)]
```

- [ ] **Step 4: Verify the helper tests are GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_live_diagnostic_adapter.py -q -k "live_log_step"
```

Expected: 4 passed.

- [ ] **Step 5: Write the failing `_create_plan` regression test**

Add an async test that uses an invalid static planner response, real PostgreSQL Live tool definitions, a `SearchLog` definition whose server is `cls`, and trusted CLS arguments. Assert the generic plan origin remains `generic`, Runtime steps remain, exactly one `SearchLog` exists, its arguments equal the trusted scope, and total steps are at most four. Add a neighbor test with the same discovered tool but no trusted binding and assert no Log step is added.

```python
from collections.abc import Mapping, Sequence


class InvalidPlanChatModel:
    async def ainvoke(self, prompt: object) -> str:
        del prompt
        return "not-json"


class InvalidPlanLlmProvider:
    def create_chat_model(self) -> InvalidPlanChatModel:
        return InvalidPlanChatModel()


def _cls_search_definition() -> McpToolDefinition:
    return McpToolDefinition(
        "SearchLog",
        "Search scoped CLS logs.",
        {
            "type": "object",
            "properties": {
                "Region": {"type": "string"},
                "TopicId": {"type": "string"},
                "From": {"type": "integer"},
                "To": {"type": "integer"},
                "Query": {"type": "string"},
                "Limit": {"type": "integer"},
            },
            "required": ["Region", "TopicId", "From", "To", "Query", "Limit"],
            "additionalProperties": False,
        },
        "cls",
    )


async def _postgres_cls_service_and_definitions(
    *, trusted_arguments: Mapping[str, object] | None
) -> tuple[AiopsDiagnosticService, tuple[McpToolDefinition, ...]]:
    runtime = tuple(
        await LivePostgresEvidenceMcpClient(_observation()).discover_tools()
    )
    service = AiopsDiagnosticService(
        repositories=cast(Any, object()),
        llm_provider=cast(Any, InvalidPlanLlmProvider()),
        retrieval_tool=cast(Any, object()),
        mcp_client=cast(Any, object()),
        cls_region="ap-guangzhou",
        cls_topic_id="topic-live",
        trusted_tool_arguments=(
            {"SearchLog": trusted_arguments}
            if trusted_arguments is not None
            else None
        ),
    )
    return service, (*runtime, _cls_search_definition())


@pytest.mark.asyncio
async def test_postgres_generic_plan_keeps_runtime_and_adds_scoped_cls_log() -> None:
    trusted = {
        "Region": "ap-guangzhou",
        "TopicId": "topic-live",
        "From": 100,
        "To": 200,
        "Query": 'incident_id:"incident-public"',
        "Limit": 20,
    }
    service, definitions = await _postgres_cls_service_and_definitions(
        trusted_arguments=trusted
    )

    plan, origin = await service._create_plan(  # pyright: ignore[reportPrivateUsage]
        query="Investigate the public incident evidence.",
        alert={"name": "PostgresOrderUpdateLatencyHigh", "severity": "warning"},
        sop_hits=(),
        no_sop_matched=True,
        tool_definitions=definitions,
        known_hypotheses=(
            "postgres_lock_blocking",
            "postgres_slow_query_without_lock",
            "postgres_connectivity_failure",
        ),
    )

    assert origin == "generic"
    assert len(plan) <= 4
    assert any(step["tool"] == "InspectPostgresSessions" for step in plan)
    log_steps = [step for step in plan if step["tool"] == "SearchLog"]
    assert len(log_steps) == 1
    assert log_steps[0]["arguments"] == trusted


@pytest.mark.asyncio
async def test_discovered_cls_tool_without_trusted_scope_is_not_forced_into_plan() -> None:
    service, definitions = await _postgres_cls_service_and_definitions(
        trusted_arguments=None
    )

    plan, origin = await service._create_plan(  # pyright: ignore[reportPrivateUsage]
        query="Investigate public evidence.",
        alert={"name": "PostgresOrderUpdateLatencyHigh", "severity": "warning"},
        sop_hits=(),
        no_sop_matched=True,
        tool_definitions=definitions,
        known_hypotheses=(
            "postgres_lock_blocking",
            "postgres_slow_query_without_lock",
            "postgres_connectivity_failure",
        ),
    )

    assert origin == "generic"
    assert all(step["tool"] != "SearchLog" for step in plan)
```

- [ ] **Step 6: Run the regression test and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_live_diagnostic_adapter.py::test_postgres_generic_plan_keeps_runtime_and_adds_scoped_cls_log -q
```

Expected: FAIL because the generic PostgreSQL plan contains no `SearchLog`.

- [ ] **Step 7: Integrate the helper before contract normalization**

In `_create_plan`, build `search_step` only when the exact discovered definition is `SearchLog` from server `cls`, trusted arguments exist, and a one-step normalization accepts the binding against the real schema. Merge that already-normalized step into both generic and successfully parsed model plans, then run the existing normalization for the complete plan:

```python
trusted_search_definition = next(
    (
        definition
        for definition in tool_definitions
        if definition.name == "SearchLog" and definition.server_name == "cls"
    ),
    None,
)
search_step: JsonDict | None = None
if (
    trusted_search_definition is not None
    and "SearchLog" in self._trusted_tool_arguments
):
    accepted_search, _search_contract_errors = normalize_tool_plan_steps(
        [self._generic_search_log_step(query)],
        trusted_tool_arguments=self._trusted_tool_arguments,
        tool_argument_contracts=self._tool_argument_contracts,
        tool_definitions=(trusted_search_definition,),
    )
    if len(accepted_search) == 1:
        search_step = accepted_search[0]
generic_plan = merge_live_log_plan_step(generic_plan, search_step=search_step)
generic_plan, _generic_contract_errors = normalize_tool_plan_steps(
    generic_plan,
    trusted_tool_arguments=self._trusted_tool_arguments,
    tool_argument_contracts=self._tool_argument_contracts,
    tool_definitions=tool_definitions,
)

# After parsing the model response and before its normalize_tool_plan_steps call.
# Keep an invalid/empty model plan empty so the complete generic plan remains the fallback.
if plan:
    plan = merge_live_log_plan_step(plan, search_step=search_step)
plan, _contract_errors = normalize_tool_plan_steps(
    plan,
    trusted_tool_arguments=self._trusted_tool_arguments,
    tool_argument_contracts=self._tool_argument_contracts,
    tool_definitions=tool_definitions,
)
```

Do not create a step from `SearchLogs`, an untrusted server, an absent binding or a binding rejected by its JSON Schema. A full four-step plan remains unchanged and safely stays Single instead of silently dropping Runtime evidence.

- [ ] **Step 8: Verify Task 2 GREEN and nearby contracts**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_live_diagnostic_adapter.py -q -k "live_log_step or postgres_generic_plan_keeps_runtime or discovered_cls_tool_without_trusted_scope or model_search_plan_is_bound or model_plan_arguments"
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit Task 2**

```powershell
git add -- apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_live_diagnostic_adapter.py
git commit -m "fix: merge cls evidence into live plans"
```

### Task 3: Prove PostgreSQL Lock + CLS can select effective Multi

**Files:**
- Test: `apps/backend/tests/test_live_diagnostic_adapter.py`
- Update documentation: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: `merge_live_log_plan_step`, `build_investigator_capabilities`, `normalize_plan_source_domains`, `route_investigation`, `InvestigationRoutingInput`, `InvestigationRouterPolicy`, and `TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES`.
- Produces: an offline scenario acceptance test proving `selected_investigators == ("runtime", "log")` under Benchmark forced Multi.

- [ ] **Step 1: Write the offline scenario route test**

Reuse the actual `_create_plan` output from Task 2, normalize domains with the real PostgreSQL and CLS tool definitions, and route with bounded public inputs. Add a permanent negative case showing that a discovered CLS tool without trusted scope remains Single:

```python
from super_ai.aiops.investigation import (
    InvestigationRoute,
    InvestigationRouterPolicy,
    InvestigationRoutingInput,
    TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
    build_investigator_capabilities,
    normalize_plan_source_domains,
    route_investigation,
)


def _route_public_postgres_plan(
    plan: Sequence[Mapping[str, object]],
    definitions: tuple[McpToolDefinition, ...],
) -> InvestigationRoute:
    capabilities = build_investigator_capabilities(
        discovered_tools=definitions,
        trusted_tool_capabilities=TRUSTED_DIAGNOSTIC_TOOL_CAPABILITIES,
        tool_policies={},
        retrieval_available=True,
        cls_available=True,
    )
    normalized = normalize_plan_source_domains(plan, capabilities)
    required_domains = frozenset(
        cast(Any, step["sourceDomain"])
        for step in normalized
        if step.get("sourceDomain") in {"runtime", "log"}
    )
    return route_investigation(
        InvestigationRoutingInput(
            required_domains=cast(Any, required_domains),
            unresolved_hypothesis_count=3,
            causal_component_count=1,
            missing_causal_roles=frozenset({"trigger", "mechanism"}),
            high_quality_conflict=False,
            severity="warning",
            trusted_pattern_matched=False,
            decision_ready=False,
            valid_tool_calls_without_gain=0,
            knowledge_hit=True,
            remaining_time_ms=90_000,
            remaining_model_calls=8,
            completed_dispatch_keys=frozenset(),
            evidence_snapshot_hash="a" * 64,
            wave=0,
        ),
        capabilities=capabilities,
        policy=InvestigationRouterPolicy(multi_agent_enabled=True),
        mode="multi",
    )


async def _planned_public_postgres_route(*, trusted: bool) -> InvestigationRoute:
    arguments = {
        "Region": "ap-guangzhou",
        "TopicId": "topic-live",
        "From": 100,
        "To": 200,
        "Query": 'incident_id:"incident-public"',
        "Limit": 20,
    }
    service, definitions = await _postgres_cls_service_and_definitions(
        trusted_arguments=arguments if trusted else None
    )
    plan, _origin = await service._create_plan(  # pyright: ignore[reportPrivateUsage]
        query="Investigate public incident evidence.",
        alert={"name": "PostgresOrderUpdateLatencyHigh", "severity": "warning"},
        sop_hits=(),
        no_sop_matched=True,
        tool_definitions=definitions,
        known_hypotheses=(
            "postgres_lock_blocking",
            "postgres_slow_query_without_lock",
            "postgres_connectivity_failure",
        ),
    )
    return _route_public_postgres_plan(plan, definitions)


@pytest.mark.asyncio
async def test_postgres_lock_cls_public_plan_can_select_effective_multi() -> None:
    route = await _planned_public_postgres_route(trusted=True)

    assert route.strategy == "multi_agent"
    assert route.selected_investigators == ("runtime", "log")
    assert "insufficient_parallel_sources" not in route.reason_codes


@pytest.mark.asyncio
async def test_postgres_lock_without_trusted_cls_scope_stays_single() -> None:
    route = await _planned_public_postgres_route(trusted=False)

    assert route.strategy == "single_agent"
    assert route.selected_investigators == ()
    assert "insufficient_parallel_sources" in route.reason_codes
```

The test file must not import `load_live_oracle`, open `ground_truth.yaml`, or pass scenario IDs/private labels into `InvestigationRoutingInput`.

- [ ] **Step 2: Run the scenario route test**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_live_diagnostic_adapter.py::test_postgres_lock_cls_public_plan_can_select_effective_multi tests/test_live_diagnostic_adapter.py::test_postgres_lock_without_trusted_cls_scope_stays_single -q
```

Expected after Tasks 1-2: 2 passed. The permanent negative test proves regression sensitivity without mutating production code during verification; the existing unknown-server test separately proves that an untrusted Runtime server cannot form Multi.

- [ ] **Step 3: Record the bounded conclusion**

Append a short dated note to `docs/aiops/agentpy-domainbench.md` stating:

```markdown
### PostgreSQL CLS Multi 离线路由回归（2026-08-20）

公开 PostgreSQL Lock hypotheses、项目内 `docker-live-postgres` Runtime 工具和受作用域约束的
CLS `SearchLog` 可形成 `runtime + log` 两个可信 Dispatch；Benchmark forced Multi 的离线路由结果为
`multi_agent`。该测试不调用 Oracle、真实 LLM 或 CLS，只证明路由可用性；真实能力增益与生产默认启用
仍需完整 A/B 门禁。
```

- [ ] **Step 4: Run the targeted regression set**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_aiops_investigation_router.py tests/test_live_diagnostic_adapter.py tests/test_aiops_multi_agent_runtime.py tests/test_aiops_v4_workflow.py -q
```

Expected: all selected tests pass; no full pytest is run.

- [ ] **Step 5: Run static verification**

```powershell
.\.venv\Scripts\python.exe -m ruff check src/super_ai/aiops/investigation.py src/super_ai/aiops/diagnostics.py tests/test_aiops_investigation_router.py tests/test_live_diagnostic_adapter.py
.\.venv\Scripts\python.exe -m pyright src/super_ai/aiops/investigation.py src/super_ai/aiops/diagnostics.py tests/test_aiops_investigation_router.py tests/test_live_diagnostic_adapter.py
git diff --check
```

Expected: Ruff exits 0, Pyright reports 0 errors, and `git diff --check` exits 0.

- [ ] **Step 6: Commit Task 3**

```powershell
git add -- apps/backend/tests/test_live_diagnostic_adapter.py docs/aiops/agentpy-domainbench.md
git commit -m "test: prove postgres cls multi routing"
```

## Final Verification

- [ ] Confirm `git status --short` is clean.
- [ ] Confirm no `config/project.json`, API key, credential, `var/` or Archive file appears in the commits.
- [ ] Report separately: offline effective Multi route proven; real A/B capability gain not yet claimed.
