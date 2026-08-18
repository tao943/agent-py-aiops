# Validator、过程评分与根因语义修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正 Snapshot 过程评分，安全细分 LLM Validator 调用失败，并让 APY-013 的 trigger 与 causal chain 按隔离的语义合同评分。

**Architecture:** 在 Artifact 层集中定义工具 Observation 角色，评分器只比较已完成诊断工具与 Evidence Evaluation。Validator 继续使用现有 LangChain structured output，但通过项目内适配器把 SDK 异常转换为允许列表审计字段。Snapshot 与 Live 共享 evaluator-only 语义 rubric 和有序里程碑评分，Agent 生成端只读取公开 Observation，不接触 Oracle。

**Tech Stack:** Python 3.10、Pydantic v2、LangChain 1.3、langchain-openai 1.3、OpenAI Python 2.44、Pytest、Ruff、Pyright、YAML Benchmark。

## Global Constraints

- PostgreSQL-only；不新增数据库或运行时外部服务。
- 配置只从 `config/project.json` 与被 Git 忽略的 `config/user.project.json` 读取，不读取环境变量。
- 不保存 Prompt、异常消息、响应正文、headers、URL query、API Key、Ground Truth、Oracle 或私有推理。
- `validationErrorCategory=model_call_failed`、`deterministic_grounded_fallback`、`manual_review` 与 `executionPermitted=false` 的既有安全语义保持兼容。
- 不改变 Benchmark 总权重、通过阈值、安全 hard gate 或历史 Archive 原始结果。
- Agent、Prompt、RAG 和报告生成路径不得读取 `ground_truth.yaml` 或 evaluator-only semantic rubric。
- 真实 APY-013 只在全部离线门通过后运行一次，不自动重试整条 Benchmark。
- 实现阶段由主 Agent 内联执行；除本计划的一次只读评审外不再启动子 Agent。

---

## File Structure

- Modify `apps/backend/src/super_ai/evaluation/artifacts.py`: 定义单一来源的工具 Observation 角色分类。
- Modify `apps/backend/src/super_ai/evaluation/scoring.py`: 使用诊断工具数量计算过程分，并接入共享根因语义评分。
- Modify `apps/backend/tests/test_evaluation_scoring.py`: 过程口径和 Snapshot 语义评分回归。
- Modify `apps/backend/src/super_ai/aiops/decision_validation.py`: 安全异常分类、调用阶段和结构化 outcome。
- Modify `apps/backend/src/super_ai/aiops/diagnostics.py`: 持久化允许列表 Validator 字段并收紧 Decision Prompt/fallback。
- Modify `apps/backend/src/super_ai/aiops/reasoning.py`: 为公开 Observation 增加非私有因果角色。
- Modify `apps/backend/tests/test_aiops_decision_validation.py`: SDK 与非标准异常分类单元测试。
- Modify `apps/backend/tests/test_aiops_reasoning_trace.py`: Workflow 字段、安全降级和 APY-013 生成回归。
- Create `apps/backend/src/super_ai/evaluation/semantic_scoring.py`: Snapshot/Live 共用的 evaluator-only 语义评分纯函数。
- Modify `apps/backend/src/super_ai/evaluation/live/semantic_scoring.py`: 兼容导出共享实现，避免现有 import 断裂。
- Modify `apps/backend/src/super_ai/evaluation/scenarios.py`: 通用 Oracle semantic rubric 严格加载。
- Modify `apps/backend/src/super_ai/evaluation/live/scenarios.py`: 复用通用 rubric loader，仅保留 Live recovery expectation。
- Modify `apps/backend/tests/test_evaluation_scenarios.py`: Snapshot rubric 加载、非法 rubric 与隔离测试。
- Modify `apps/backend/tests/test_live_evaluation_scenarios.py`: 共享 loader 兼容回归。
- Modify `benchmarks/agentpy/scenarios/APY-013/ground_truth.yaml`: 增加 evaluator-only trigger concepts 与三个 causal milestones。
- Modify `apps/backend/pyproject.toml`: 将已锁定的 OpenAI Python SDK 声明为直接依赖。
- Modify `apps/backend/uv.lock`: 同步直接依赖关系，不改变已锁定 `openai==2.44.0`。
- Modify `docs/aiops/agentpy-domainbench.md`: 记录评分口径、脱敏字段和新验收结果。

### Task 1: 修正 observations_evaluated 工具边界

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/reasoning.py`
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/src/super_ai/evaluation/scoring.py`
- Test: `apps/backend/tests/test_evaluation_scoring.py`

**Interfaces:**
- Produces: `ToolObservationRole = Literal["diagnostic_observation", "knowledge_context", "recovery_or_verification", "unknown"]`。
- Produces: `tool_observation_role(tool: ArtifactToolCall) -> ToolObservationRole`。
- Extends: `ArtifactToolCall.audit_id`、`ArtifactEvidence.tool_call_id`、`ObservationDecision.evidence_ids`，用于逐调用覆盖。
- Consumes: `RunArtifact.tool_calls`、`RunArtifact.evidence` 与 `RunArtifact.observation_decisions`。

- [ ] **Step 1: 写出错误口径的失败测试**

在 `test_evaluation_scoring.py` 增加：

```python
def test_process_score_ignores_knowledge_retrieval_observation_count() -> None:
    artifact = process_down_artifact()
    with_rag = replace(
        artifact,
        tool_calls=(
            ArtifactToolCall("knowledge_retrieval", "completed", "L0"),
            *artifact.tool_calls,
        ),
    )

    result = score_run(with_rag, process_down_oracle())
    reason = next(item for item in result.reasons if item.code == "observations_evaluated")

    assert reason.points == 5


def test_process_score_fails_when_diagnostic_observation_is_missing() -> None:
    artifact = process_down_artifact()
    missing = replace(artifact, observation_decisions=artifact.observation_decisions[:1])

    result = score_run(missing, process_down_oracle())
    reason = next(item for item in result.reasons if item.code == "observations_evaluated")

    assert reason.points == 0


def test_process_score_fails_closed_for_unknown_completed_l0_tool() -> None:
    artifact = process_down_artifact()
    unknown = replace(
        artifact,
        tool_calls=artifact.tool_calls
        + (ArtifactToolCall("InspectFutureSubsystem", "completed", "L0"),),
    )

    result = score_run(unknown, process_down_oracle())
    reason = next(item for item in result.reasons if item.code == "observations_evaluated")

    assert reason.points == 0


def test_process_score_rejects_duplicate_observation_coverage() -> None:
    base = process_down_artifact()
    artifact = replace(
        base,
        tool_calls=(
            replace(base.tool_calls[0], audit_id="call-1"),
            replace(base.tool_calls[1], audit_id="call-2"),
            ArtifactToolCall("InspectPostgresErrors", "completed", "L0", audit_id="call-3"),
            ArtifactToolCall("InspectPostgresWaitGraph", "completed", "L0", audit_id="call-4"),
        ),
        evidence=(
            replace(base.evidence[0], tool_call_id="call-1"),
            replace(base.evidence[1], tool_call_id="call-2"),
            ArtifactEvidence("ev-3", "claim-3", True, tool_call_id="call-3"),
            ArtifactEvidence("ev-4", "claim-4", True, tool_call_id="call-4"),
        ),
        observation_decisions=(
            replace(base.observation_decisions[0], evidence_ids=("ev-container",)),
            replace(base.observation_decisions[1], evidence_ids=("ev-nginx",)),
            replace(base.observation_decisions[0], evidence_ids=("ev-3",)),
            replace(base.observation_decisions[1], evidence_ids=("ev-4",)),
        ),
    )
    first = artifact.observation_decisions[0]
    duplicate = replace(
        artifact,
        observation_decisions=(*artifact.observation_decisions[:3], first),
    )

    result = score_run(duplicate, process_down_oracle())
    reason = next(item for item in result.reasons if item.code == "observations_evaluated")

    assert len(duplicate.observation_decisions) == 4
    assert reason.points == 0
```

- [ ] **Step 2: 运行测试确认旧实现失败**

Run:

```powershell
uv run pytest tests/test_evaluation_scoring.py -q
```

Expected: RAG 用例得到 `0` 而非 `5`，unknown L0 用例错误通过。

- [ ] **Step 3: 在 Artifact 层实现唯一工具角色表**

在 `artifacts.py` 添加并导出：

```python
ToolObservationRole = Literal[
    "diagnostic_observation",
    "knowledge_context",
    "recovery_or_verification",
    "unknown",
]

_KNOWLEDGE_CONTEXT_TOOLS = frozenset(
    {"knowledge_retrieval", "SearchKnowledge", "GetActiveAlerts"}
)
_RECOVERY_OR_VERIFICATION_TOOLS = frozenset(
    {
        "VerifyServiceHealth",
        "RestartTestService",
        "ResumeTestConsumer",
        "DeleteRebuildableTestCacheKey",
        "RestoreTestRedisService",
        "RemoveInjectedNetworkFault",
        "RestoreInjectedServiceState",
    }
)
_DIAGNOSTIC_OBSERVATION_TOOLS = frozenset(
    {
        "SearchLogs",
        "SearchLog",
        "QueryMetrics",
        "QueryTrace",
        "GetDatabaseMetrics",
        "InspectContainer",
        "GetGatewayMetrics",
        "GetRedisConnectionMetrics",
        "GetServiceMetrics",
        "InspectClientRetryPolicy",
        "InspectDatabasePool",
        "InspectGatewayErrors",
        "InspectGatewayRequestTimeline",
        "InspectHostLimits",
        "InspectHttpAttempts",
        "InspectNginx",
        "InspectPostgres",
        "InspectPostgresErrors",
        "InspectPostgresWaitGraph",
        "InspectPostgresSessions",
        "InspectPostgresLockGraph",
        "InspectRateLimitTimeline",
        "InspectRedis",
        "InspectRedisClientPool",
        "InspectRedisServer",
        "InspectTrafficAndDependencyHealth",
        "InspectTransactionResourceOrder",
        "ListRedisClients",
        "ProbeUpstreamHealth",
        "GetServiceTopology",
        "GetDeploymentChanges",
    }
)


def tool_observation_role(tool: ArtifactToolCall) -> ToolObservationRole:
    if tool.name in _KNOWLEDGE_CONTEXT_TOOLS:
        return "knowledge_context"
    if tool.name in _RECOVERY_OR_VERIFICATION_TOOLS or tool.name.startswith("Propose"):
        return "recovery_or_verification"
    if tool.name in _DIAGNOSTIC_OBSERVATION_TOOLS:
        return "diagnostic_observation"
    return "unknown"
```

同时给 `ArtifactToolCall` 增加尾部默认字段 `audit_id: str | None = None`，给 `ArtifactEvidence` 增加尾部默认字段 `tool_call_id: str | None = None`，给 `ObservationDecision` 增加尾部默认字段 `evidence_ids: tuple[str, ...] = ()`。生产 Artifact 分别从工具审计 `id`、Evidence `tool_call_id` 与 Observation payload `evidenceIds` 填充；历史构造保持兼容。让 `_risk_tier` 复用角色集合，避免名称在两个独立列表漂移。

- [ ] **Step 4: 按角色重写过程评分**

在 `scoring.py` 使用：

```python
completed = tuple(item for item in artifact.tool_calls if item.status == "completed")
diagnostic = tuple(
    item for item in completed if tool_observation_role(item) == "diagnostic_observation"
)
unknown_l0 = any(
    item.risk_tier == "L0" and tool_observation_role(item) == "unknown"
    for item in completed
)
evidence_to_tool = {
    item.record_id: item.tool_call_id
    for item in artifact.evidence
    if item.tool_call_id is not None
}
covered_tool_calls = {
    tool_call_id
    for observation in artifact.observation_decisions
    for evidence_id in observation.evidence_ids
    if (tool_call_id := evidence_to_tool.get(evidence_id)) is not None
}
diagnostic_ids = {item.audit_id for item in diagnostic if item.audit_id is not None}
all_diagnostics_linked = bool(diagnostic) and all(
    item.audit_id is not None for item in diagnostic
)
linked_complete = all_diagnostics_linked and diagnostic_ids <= covered_tool_calls
legacy_complete = (
    bool(diagnostic)
    and all(item.audit_id is None for item in diagnostic)
    and len(artifact.observation_decisions) >= len(diagnostic)
)
observations_complete = (linked_complete or legacy_complete) and not unknown_l0
```

将 `observations_complete` 传给现有 `_award(..., "observations_evaluated", 5, ...)`；权重不变。新生产 Artifact 必须走 ID 关联；只有全部 ID 缺失的历史/手工 Artifact 才使用兼容计数，禁止部分有 ID 时回退。

- [ ] **Step 5: 运行专项测试并提交**

Run:

```powershell
uv run pytest tests/test_evaluation_scoring.py tests/test_evaluation_artifacts.py -q
```

Expected: PASS。

Commit:

```powershell
git add -- apps/backend/src/super_ai/aiops/reasoning.py apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/src/super_ai/evaluation/scoring.py apps/backend/tests/test_evaluation_scoring.py
git commit -m "fix: score diagnostic observations by tool role"
```

### Task 2: 增加 Validator 脱敏失败分类

**Files:**
- Modify: `apps/backend/pyproject.toml`
- Modify: `apps/backend/uv.lock`
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Test: `apps/backend/tests/test_aiops_decision_validation.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `ValidationErrorCode`、`ValidationErrorPhase` 与 `SafeModelFailure`。
- Produces: `classify_model_failure(exc: Exception, *, phase: ValidationErrorPhase) -> SafeModelFailure`。
- Extends: `StructuredValidationOutcome` with `error_code`, `error_phase`, `retryable`, `http_status_class`。
- Persists: `validationErrorCode`、`validationErrorPhase`、`validationRetryable`、`validationHttpStatusClass`。

- [ ] **Step 1: 把已安装 SDK 声明为直接依赖**

在 `pyproject.toml` 的 dependencies 中加入：

```toml
"openai==2.44.0",
```

Run:

```powershell
uv lock
```

Expected: `uv.lock` 中仍为 `openai==2.44.0`，项目依赖元数据只增加 direct dependency，不下载新供应商 SDK。运行 `git diff -- apps/backend/uv.lock`，确认没有其他包版本变化。

- [ ] **Step 2: 写异常分类失败测试**

在 `test_aiops_decision_validation.py` 构造 `httpx.Request/Response`，参数化断言：

```python
def _status_error(
    error_type: type[openai.APIStatusError],
    status_code: int,
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return error_type("secret provider body", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "code", "retryable", "status_class"),
    (
        (TimeoutError("secret timeout text"), "timeout", True, None),
        (openai.APIConnectionError(request=httpx.Request("POST", "https://provider.test")), "connection", True, None),
        (_status_error(openai.AuthenticationError, 401), "authentication", False, "4xx"),
        (_status_error(openai.PermissionDeniedError, 403), "permission_denied", False, "4xx"),
        (_status_error(openai.RateLimitError, 429), "rate_limit", True, "4xx"),
        (_status_error(openai.BadRequestError, 400), "provider_4xx", False, "4xx"),
        (_status_error(openai.InternalServerError, 500), "provider_5xx", True, "5xx"),
        (RuntimeError("api-key-and-response-body"), "unknown", False, None),
    ),
)
def test_model_failure_classification_is_allowlisted(
    error: Exception,
    code: str,
    retryable: bool,
    status_class: str | None,
) -> None:
    result = classify_model_failure(error, phase="model_invoke")

    assert result.code == code
    assert result.retryable is retryable
    assert result.http_status_class == status_class
    assert "api-key" not in repr(result)
    assert "response-body" not in repr(result)
```

另加两个 structured setup 抛错测试：`NotImplementedError` 期望 `structured_output_unsupported / structured_invoker_setup / retryable=False`；`RuntimeError("secret setup failure")` 期望 `unknown / structured_invoker_setup / retryable=False` 且不出现消息正文。

- [ ] **Step 3: 运行测试确认分类器不存在**

Run:

```powershell
uv run pytest tests/test_aiops_decision_validation.py -q
```

Expected: collection/import FAIL，因为 `classify_model_failure` 和新 outcome 字段尚不存在。

- [ ] **Step 4: 实现只读取类型和状态段的分类器**

在 `decision_validation.py` 添加：

```python
ValidationErrorCode = Literal[
    "timeout",
    "connection",
    "authentication",
    "permission_denied",
    "rate_limit",
    "provider_4xx",
    "provider_5xx",
    "structured_output_unsupported",
    "unknown",
]
ValidationErrorPhase = Literal[
    "structured_invoker_setup",
    "model_invoke",
    "structured_parse",
]


@dataclass(frozen=True, slots=True)
class SafeModelFailure:
    code: ValidationErrorCode
    phase: ValidationErrorPhase
    retryable: bool
    http_status_class: Literal["4xx", "5xx"] | None = None


def classify_model_failure(
    exc: Exception,
    *,
    phase: ValidationErrorPhase,
) -> SafeModelFailure:
    if phase == "structured_invoker_setup" and isinstance(
        exc, (NotImplementedError, TypeError)
    ):
        return SafeModelFailure("structured_output_unsupported", phase, False)
    if isinstance(exc, (TimeoutError, openai.APITimeoutError)):
        return SafeModelFailure("timeout", phase, True)
    if isinstance(exc, openai.AuthenticationError):
        return SafeModelFailure("authentication", phase, False, "4xx")
    if isinstance(exc, openai.PermissionDeniedError):
        return SafeModelFailure("permission_denied", phase, False, "4xx")
    if isinstance(exc, openai.RateLimitError):
        return SafeModelFailure("rate_limit", phase, True, "4xx")
    if isinstance(exc, openai.APIConnectionError):
        return SafeModelFailure("connection", phase, True)
    if isinstance(exc, openai.APIStatusError):
        status_class = "5xx" if exc.status_code >= 500 else "4xx"
        return SafeModelFailure(
            "provider_5xx" if status_class == "5xx" else "provider_4xx",
            phase,
            status_class == "5xx",
            status_class,
        )
    return SafeModelFailure("unknown", phase, False)
```

不得调用 `str(exc)`，不得保存精确 status code。

- [ ] **Step 5: 将 setup、invoke、parse 放进不同边界**

重构 `invoke_structured_root_cause_validation`：先捕获 `_structured_invoker(model)` 的 setup 异常；只有 `NotImplementedError/TypeError` 标为不支持，其他 setup 异常安全降级为 `unknown`；调用异常进入 `model_invoke`；envelope/Pydantic/业务解析继续使用 `invalid_json_or_schema`，并把 phase 标记为 `structured_parse`。所有返回分支都完整初始化新增字段。

调用失败返回形态必须等价于：

```python
failure = classify_model_failure(exc, phase="model_invoke")
return StructuredValidationOutcome(
    decision=None,
    error_category="model_call_failed",
    attempts=attempt,
    error_codes=(failure.code,),
    error_code=failure.code,
    error_phase=failure.phase,
    retryable=failure.retryable,
    http_status_class=failure.http_status_class,
)
```

- [ ] **Step 6: 持久化安全字段并证明不改变恢复授权**

在 `diagnostics.py` 把 outcome 字段写入 step、checkpoint 和 state payload：

```python
"validationErrorCode": validation_error_code,
"validationErrorPhase": validation_error_phase,
"validationRetryable": validation_retryable,
"validationHttpStatusClass": validation_http_status_class,
```

更新 APY-013 unavailable-validator Workflow 测试，断言：

```python
assert validation.payload["validationErrorCategory"] == "model_call_failed"
assert validation.payload["validationErrorCode"] == "timeout"
assert validation.payload["validationErrorPhase"] == "model_invoke"
assert validation.payload["validationRetryable"] is True
assert validation.payload["validationHttpStatusClass"] is None
assert validation.payload["validationOrigin"] == "deterministic_grounded_fallback"
assert recovery.payload["mode"] == "manual_review"
assert policy.payload["executionPermitted"] is False
```

- [ ] **Step 7: 运行 Validator 与 Workflow 专项测试并提交**

Run:

```powershell
uv run pytest tests/test_aiops_decision_validation.py tests/test_aiops_reasoning_trace.py -q
```

Expected: PASS，且测试中搜索的 secret marker 未进入持久化 payload。

Commit:

```powershell
git add -- apps/backend/pyproject.toml apps/backend/uv.lock apps/backend/src/super_ai/aiops/decision_validation.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_decision_validation.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "fix: classify validator failures safely"
```

### Task 3: 共享并强化 evaluator-only 语义评分

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/semantic_scoring.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/semantic_scoring.py`
- Modify: `apps/backend/src/super_ai/evaluation/scenarios.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/scenarios.py`
- Modify: `apps/backend/src/super_ai/evaluation/scoring.py`
- Modify: `benchmarks/agentpy/scenarios/APY-013/ground_truth.yaml`
- Test: `apps/backend/tests/test_evaluation_scenarios.py`
- Test: `apps/backend/tests/test_evaluation_scoring.py`
- Test: `apps/backend/tests/test_live_evaluation_scenarios.py`
- Test: `apps/backend/tests/test_live_evaluation_scoring.py`

**Interfaces:**
- Produces: `load_root_cause_semantics(payload: Mapping[str, object]) -> RootCauseSemantics` in `evaluation.scenarios`。
- Produces: `score_root_cause_semantics(decision, oracle) -> RootCauseSemanticScore` in `evaluation.semantic_scoring`。
- Compatibility: `evaluation.live.semantic_scoring` re-exports both public names。

- [ ] **Step 1: 写 Snapshot 同义、缺环和乱序失败测试**

在 `test_evaluation_scoring.py` 为 APY-013 构造 Decision：

```python
def test_snapshot_semantic_score_accepts_grounded_apy_013_paraphrase() -> None:
    artifact = apy_013_artifact(
        trigger="Concurrent order transactions acquire order and inventory rows in reverse order.",
        causal_chain=(
            "Two transactions acquired shared rows in opposite orders.",
            "Each transaction waited for a row lock held by the other.",
            "PostgreSQL detected the deadlock cycle and aborted one with SQLSTATE 40P01.",
        ),
    )

    result = score_run(artifact, load_scenario_oracle(SCENARIOS / "APY-013"))

    assert next(item for item in result.reasons if item.code == "trigger_correct").points == 3
    assert next(item for item in result.reasons if item.code == "causal_chain_correct").points == 3
```

再用同一 fixture 测试：删除 wait-cycle step 时 causal chain 得 0；交换 abort 和 wait step 时得 0；错误 mechanism 时 trigger/chain 不获得语义分。

- [ ] **Step 2: 写严格 rubric loader 与隔离失败测试**

在 `test_evaluation_scenarios.py` 断言 APY-013 加载三个里程碑，并参数化拒绝未知 concept、重复 milestone ID、空 alias 和非三个 milestone。保留并扩展现有公开序列化断言：

```python
serialized_public = json.dumps(asdict(load_public_scenario(SCENARIOS / "APY-013")))
assert "root_cause_semantics" not in serialized_public
assert "reverse_order" not in serialized_public
```

- [ ] **Step 3: 运行测试确认 Snapshot 尚不支持 rubric**

Run:

```powershell
uv run pytest tests/test_evaluation_scenarios.py tests/test_evaluation_scoring.py -q
```

Expected: APY-013 `root_cause_semantics` 未加载，逐字评分拒绝 paraphrase。

- [ ] **Step 4: 将 rubric parser 移到通用 scenario loader**

把 Live `_root_cause_semantics` 的严格实现迁移为 `evaluation.scenarios.load_root_cause_semantics`。`load_scenario_oracle` 在 YAML 存在 `root_cause_semantics` 时加载，否则保持 `None`，保证旧 Snapshot 兼容。`load_live_oracle` 继续强制 rubric 必须存在：

```python
oracle = load_scenario_oracle(path)
if oracle.root_cause_semantics is None:
    raise ValueError("Live root-cause semantic rubric is required.")
return replace(oracle, recovery_expectation=recovery_expectation)
```

- [ ] **Step 5: 建立共享且有序的 semantic scorer**

把 Live scorer 移到 `evaluation/semantic_scoring.py`，并让 milestone 只在递增 causal-chain index 中匹配：

```python
def _ordered_milestone_scores(
    steps: tuple[str, ...],
    requirements: tuple[SemanticRequirement, ...],
    semantics: RootCauseSemantics,
) -> tuple[tuple[str, int], ...]:
    next_index = 0
    scores: list[tuple[str, int]] = []
    for requirement in requirements:
        match = next(
            (
                index
                for index in range(next_index, len(steps))
                if _requirement_matches(steps[index], requirement, semantics)
            ),
            None,
        )
        scores.append((requirement.id, 2 if match is not None else 0))
        if match is not None:
            next_index = match + 1
    return tuple(scores)
```

`evaluation.live.semantic_scoring` 只兼容导出：

```python
from super_ai.evaluation.semantic_scoring import (
    RootCauseSemanticScore,
    score_root_cause_semantics,
)

__all__ = ["RootCauseSemanticScore", "score_root_cause_semantics"]
```

- [ ] **Step 6: 在 Snapshot 保持 25 分维度但替换 6 分语义判断**

在 `_score_diagnosis` 中：component 5 分、mechanism 10 分与 rule-out 2 分保持不变；若 `oracle.root_cause_semantics` 存在，则用共享 scorer 的 trigger/milestones 布尔完整性映射回现有 trigger 3 分与 causal chain 3 分。没有 rubric 的旧场景继续精确匹配。

```python
semantic = (
    score_root_cause_semantics(decision, oracle)
    if oracle.root_cause_semantics is not None
    else None
)
trigger_correct = (
    semantic.trigger == 4
    if semantic is not None
    else decision.trigger == oracle.primary_cause.trigger
)
causal_chain_correct = (
    all(points == 2 for _, points in semantic.milestones)
    if semantic is not None
    else decision.causal_chain == oracle.causal_chain
)
```

- [ ] **Step 7: 为 APY-013 添加 evaluator-only rubric**

在 `ground_truth.yaml` 添加：

```yaml
root_cause_semantics:
  concepts:
    transaction:
      - transaction
      - transactions
    order_resource:
      - order row
      - order rows
      - order resource
      - order resources
    inventory_resource:
      - inventory row
      - inventory rows
      - inventory resource
      - inventory resources
    opposite_order:
      - opposite order
      - opposite orders
      - reverse order
      - reversed order
    wait_cycle:
      - wait cycle
      - waiting cycle
      - cyclic wait
      - waited for a row lock held by the other
    postgresql:
      - postgresql
      - postgres
    abort:
      - abort
      - aborted
      - rollback
      - rolled back
    sqlstate_40p01:
      - sqlstate 40p01
      - 40p01
  trigger:
    all_of: [transaction, order_resource, inventory_resource, opposite_order]
  causal_milestones:
    - id: opposite_resource_acquisition
      all_of: [transaction, order_resource, inventory_resource, opposite_order]
    - id: cyclic_lock_wait
      all_of: [transaction, wait_cycle]
    - id: postgres_deadlock_abort
      all_of: [postgresql, abort, sqlstate_40p01]
```

不得把这些字段复制到 `scenario.yaml`、RAG 卡片或 Decision Prompt。

- [ ] **Step 8: 运行 Snapshot 与 Live 语义回归并提交**

Run:

```powershell
uv run pytest tests/test_evaluation_scenarios.py tests/test_evaluation_scoring.py tests/test_live_evaluation_scenarios.py tests/test_live_evaluation_scoring.py -q
```

Expected: PASS；Live 既有同义样例通过，新增乱序样例失败。

Commit:

```powershell
git add -- apps/backend/src/super_ai/evaluation/semantic_scoring.py apps/backend/src/super_ai/evaluation/live/semantic_scoring.py apps/backend/src/super_ai/evaluation/scenarios.py apps/backend/src/super_ai/evaluation/live/scenarios.py apps/backend/src/super_ai/evaluation/scoring.py apps/backend/tests/test_evaluation_scenarios.py apps/backend/tests/test_evaluation_scoring.py apps/backend/tests/test_live_evaluation_scenarios.py apps/backend/tests/test_live_evaluation_scoring.py benchmarks/agentpy/scenarios/APY-013/ground_truth.yaml
git commit -m "fix: score ordered root cause semantics"
```

### Task 4: 收紧 Decision 结构化生成合同

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/reasoning.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Test: `apps/backend/tests/test_evaluation_artifacts.py`

**Interfaces:**
- Consumes: 公开 `decisionVocabulary`、supported hypothesis 与 `observation_decisions`。
- Produces: `CausalRole = Literal["trigger", "mechanism", "impact", "context"]` 与可审计的 `ObservationDecision.causal_role`。
- Produces: 按 causal role 排序的 2–6 项 Evidence-grounded causal chain；trigger 不能从 evaluator-only Oracle 获取。

- [ ] **Step 1: 写 APY-013 prompt 与 fallback 失败测试**

扩展 `test_aiops_reasoning_trace.py`：捕获 Evidence Evaluation prompt，断言要求 `causalRole` 只能为 `trigger/mechanism/impact/context`；捕获 Decision prompt，断言包含“direct trigger condition”“2 to 6 ordered atomic causal facts”“must map to supporting structured observations”，且不含 `primary_cause`、`root_cause_semantics`、Oracle trigger token。为 fallback 按 `impact, trigger, mechanism` 的执行顺序构造三个支持性 Observation，断言输出被重排为 `trigger, mechanism, impact`，且 trigger 来自 `causalRole=trigger` 的公开 summary，而不是 Public Hypothesis 的笼统 description。再构造 8 条含重复 summary、多个 mechanism 和末端 impact 的 Observation，断言去重后不超过 6 条、trigger 为首项、至少一个 mechanism 被保留、关键 impact 为末项。

在 `test_evaluation_artifacts.py` 断言 `causalRole` 能从持久化 Evidence Evaluation 解析回 Artifact，缺失字段的历史记录默认 `context`。

- [ ] **Step 2: 运行测试确认旧 Prompt/fallback 失败**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py tests/test_evaluation_artifacts.py -q
```

Expected: Prompt 合同断言失败，Artifact 未保留 causal role，fallback trigger 仍等于 public hypothesis description。

- [ ] **Step 3: 增加公开、非私有的 Observation causal role**

在 `reasoning.py` 扩展数据合同：

```python
CausalRole = Literal["trigger", "mechanism", "impact", "context"]


@dataclass(frozen=True, slots=True)
class ObservationDecision:
    purpose: str
    supports: tuple[str, ...]
    refutes: tuple[str, ...]
    summary: str
    evidence_ids: tuple[str, ...] = ()
    causal_role: CausalRole = "context"
```

`parse_observation_decision` 接受可选 `causalRole`；缺失时使用 `context`，非法值必须拒绝。`_observation_decision_payload` 持久化为 `causalRole`。`artifacts.py` 的 `_observation_decisions_from_steps` 同样解析该字段，保证历史 payload 向后兼容。

修改 Evidence Evaluation Prompt：

```python
"Return one JSON observation decision with purpose, supports, refutes, summary, and "
"causalRole. causalRole must be one of trigger, mechanism, impact, or context and "
"describes the public causal function of this observation, not private reasoning. "
```

- [ ] **Step 4: 修改 Decision Prompt**

将 `_decision` 的固定指令改为：

```python
"Return JSON only for one root-cause decision with component, mechanism, trigger, "
"causalChain, evidenceIds, and confidence. Trigger must state the direct triggering "
"condition, not repeat the alert symptom or the broad hypothesis description. "
"causalChain must contain 2 to 6 ordered atomic causal facts, and every fact must "
"map to a supporting structured observation. This is a structured public decision, "
"not private chain-of-thought. Use only persisted evidence IDs. "
```

保留公开 Vocabulary 规范 component/mechanism 的原指令。

- [ ] **Step 5: 从 causal role 安全构造 fallback trigger 与有序 chain**

增加纯函数 `_grounded_trigger`：只查看 supported hypothesis 对应且 `causalRole=trigger` 的 Observation `summary`；要求它引用 candidate evidence。若没有唯一、非空的直接触发事实，`build_grounded_fallback_decision` 返回 `None`，不得回退到 Hypothesis description、Oracle 或编造 trigger。

把 causal role 定义为因果“阶段”而非同阶段内部先后：按 `trigger -> mechanism -> context -> impact` 排序；同一阶段是并列事实，按持久化 sequence 稳定排列并按规范化 summary 去重。最多选择一个 trigger、最多四个 mechanism、一个最终 impact；若存在 impact，必须为它预留最后一个槽位，只有容量剩余时才加入 context，禁止简单截断丢失终态。返回 Decision 前要求 causal chain 2–6 项。确定性 Validator 继续要求每个 chain item 是支持性 Observation summary，因此排序与选择不会放宽 grounded 合同。

核心实现为：

```python
_MAX_CAUSAL_CHAIN_ITEMS = 6


def _ordered_grounded_observations(
    observations: Sequence[JsonDict],
    *,
    hypothesis_id: str,
    evidence_ids: set[str],
) -> tuple[JsonDict, ...]:
    supported = [
        (index, item)
        for index, item in enumerate(observations)
        if hypothesis_id in cast(list[object], item.get("supports") or [])
        and evidence_ids.intersection(
            value
            for value in cast(list[object], item.get("evidenceIds") or [])
            if isinstance(value, str)
        )
        and isinstance(item.get("summary"), str)
        and cast(str, item["summary"]).strip()
    ]
    unique: list[JsonDict] = []
    seen: set[str] = set()
    for _, item in supported:
        key = " ".join(cast(str, item["summary"]).casefold().split())
        if key not in seen:
            seen.add(key)
            unique.append(item)
    triggers = [item for item in unique if item.get("causalRole") == "trigger"][:1]
    mechanisms = [item for item in unique if item.get("causalRole") == "mechanism"]
    impacts = [item for item in unique if item.get("causalRole") == "impact"]
    contexts = [item for item in unique if item.get("causalRole") == "context"]
    terminal = impacts[-1:] if impacts else []
    mechanism_limit = _MAX_CAUSAL_CHAIN_ITEMS - len(triggers) - len(terminal)
    selected = [*triggers, *mechanisms[:mechanism_limit]]
    context_limit = _MAX_CAUSAL_CHAIN_ITEMS - len(selected) - len(terminal)
    selected.extend(contexts[:context_limit])
    selected.extend(terminal)
    return tuple(selected)


def _grounded_trigger(observations: Sequence[JsonDict]) -> str | None:
    triggers = [
        cast(str, item["summary"]).strip()
        for item in observations
        if item.get("causalRole") == "trigger"
    ]
    return triggers[0] if len(triggers) == 1 else None
```

- [ ] **Step 6: 验证生成与确定性 Validator 一致并提交**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py tests/test_aiops_decision_validation.py tests/test_evaluation_artifacts.py -q
```

Expected: PASS；APY-013 离线链路仍通过十项确定性检查，且 Ground Truth 隔离断言通过。

Commit:

```powershell
git add -- apps/backend/src/super_ai/aiops/reasoning.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_evaluation_artifacts.py
git commit -m "fix: ground root cause trigger and causal chain"
```

### Task 5: 工程验证、真实 APY-013 与文档

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Runtime artifact only: `apps/backend/var/benchmarks/APY-013-validator-scoring-repair-real.json`
- Shared archive only: configured external Evaluation Archive directory

**Interfaces:**
- Consumes: Tasks 1–4 完成的评分、Validator 和生成合同。
- Produces: 一次真实 APY-013 可审计结果；代码和文档不包含凭据或原始供应商响应。

- [ ] **Step 1: 运行受影响后端回归**

Run:

```powershell
uv run pytest tests/test_evaluation_scoring.py tests/test_evaluation_artifacts.py tests/test_evaluation_scenarios.py tests/test_live_evaluation_scoring.py tests/test_live_evaluation_scenarios.py tests/test_aiops_decision_validation.py tests/test_aiops_reasoning_trace.py tests/test_evaluation_cli.py tests/test_snapshot_evaluation_tools.py tests/test_knowledge_candidate_safety.py -q
```

Expected: PASS。

- [ ] **Step 2: 运行静态与规格检查**

Run:

```powershell
uv run ruff check src tests
uv run pyright
openspec validate harden-aiops-decision-validation --strict
openspec validate --all --strict
```

Expected: Ruff 无错误；Pyright `0 errors`；OpenSpec focused 与 all 全部通过。

- [ ] **Step 3: 审计真实运行前置条件**

用只读查询验证 PostgreSQL 可用且 30 张知识卡均为 `ready/indexed`：

```powershell
@'
import asyncio
from sqlalchemy import func, select
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.models import KnowledgeDocumentModel

OWNER = "user_c88807ff36b74a038b9e1ea31a389cfc"
KNOWLEDGE_BASE = "kb_user_c88807ff36b74a038b9e1ea31a389cfc"

async def main() -> None:
    engine = create_memory_engine(config_path="../../config/project.json")
    sessions = create_memory_session_factory(engine)
    try:
        async with sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(KnowledgeDocumentModel)
                .where(
                    KnowledgeDocumentModel.owner_user_id == OWNER,
                    KnowledgeDocumentModel.knowledge_base_id == KNOWLEDGE_BASE,
                    KnowledgeDocumentModel.deleted_at.is_(None),
                    KnowledgeDocumentModel.status == "ready",
                    KnowledgeDocumentModel.index_status == "indexed",
                )
            )
        print({"ready_indexed_documents": count})
        if count != 30:
            raise SystemExit("Expected exactly 30 ready/indexed knowledge documents.")
    finally:
        await engine.dispose()

asyncio.run(main())
'@ | uv run python -
uv run pytest tests/test_live_llm.py -m live_llm -q
uv run python scripts/manage_evaluation_history.py audit --config ../../config/project.json
```

Expected: 文档计数为 `30`、LLM readiness PASS、Archive audit 无 checksum conflict。命令只打印计数和脱敏 readiness，不输出 `config/user.project.json` 或密钥。

- [ ] **Step 4: 只运行一次真实 APY-013**

使用已验证的非敏感 owner user ID 与 knowledge base ID 运行：

```powershell
$EvalOwnerId='user_c88807ff36b74a038b9e1ea31a389cfc'
$EvalKnowledgeBaseId='kb_user_c88807ff36b74a038b9e1ea31a389cfc'
uv run python scripts/run_snapshot_benchmark.py --scenario APY-013 --suite-version evidence-v3 --runs 1 --adapter application --config ../../config/project.json --rag-mode on --owner-user-id $EvalOwnerId --knowledge-base-id $EvalKnowledgeBaseId --output var/benchmarks/APY-013-validator-scoring-repair-real.json
```

Expected: 只生成一个新 run ID；运行终态自动写入 PostgreSQL 和共享 Archive。命令中的 PowerShell 变量只保存非敏感 ID，不承载 API Key。

- [ ] **Step 5: 验收并安全记录结果**

检查报告与持久化 step：

- `observations_evaluated` 对四次诊断调用和四条 Evidence Evaluation 得 5 分。
- Validator 成功时 `validationOrigin=llm_confirmed`；失败时 `validationErrorCode` 为允许列表值且 phase/retryable 非空。
- Artifact 不含异常消息、Prompt、响应正文、Ground Truth 或 API Key。
- Validator 失败时仍为 `manual_review` 且 `executionPermitted=false`。
- trigger/causal chain 语义得分与缺失里程碑能够逐项解释。
- Archive 与 PostgreSQL checksum 对账无 pending/conflict。

- [ ] **Step 6: 更新文档并提交**

在 `agentpy-domainbench.md` 记录 commit、run ID、耗时、分项分数、Validator 安全分类和是否通过；不得记录原始异常或真实日志正文。

Run:

```powershell
git diff --check
git status --short
```

Expected: 只有计划内代码、测试、Benchmark Oracle 和文档变更；`var/`、Archive、用户配置未被 Git 跟踪。

Commit:

```powershell
git add -- docs/aiops/agentpy-domainbench.md
git commit -m "docs: record validator scoring acceptance"
```

## Final Verification

- [ ] 重新运行 Task 5 Step 1 与 Step 2 的全部离线门，确认文档修改没有影响代码状态。
- [ ] 运行 `git status --short`，确认工作树干净。
- [ ] 运行以下 Evaluation History 命令，确认新运行已同步且无 checksum conflict：

```powershell
uv run python scripts/manage_evaluation_history.py audit --config ../../config/project.json
uv run python scripts/manage_evaluation_history.py summarize --config ../../config/project.json
```
- [ ] 不重复运行真实 APY-013；最终报告引用已产生的唯一 run ID。
