# AIOps Causal Intent Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the AIOps LangGraph establish an evidence-grounded trigger/mechanism/impact investigation structure before Decision, and make Decision generation use the existing bounded structured-output safety pattern.

**Architecture:** Add a project-owned causal-intent registry and pure coverage helpers around the existing MCP definitions rather than changing the MCP protocol. Planner and Replanner produce typed intents, plan coverage is minimally repaired before execution, Evidence Evaluation is normalized to the validated plan contract, and the Sufficiency Gate routes by deterministic causal coverage. Decision reuses the existing LangChain/Pydantic structured invoker and safe model-failure classification.

**Tech Stack:** Python 3.10, LangGraph 1.2, LangChain 1.3, Pydantic 2, OpenAI Python 2.44, Pytest, Ruff, Pyright.

## Global Constraints

- PostgreSQL-only; do not add a database, model, service, SDK, package, native binary, or runtime dependency.
- Configuration remains limited to `config/project.json` and ignored `config/user.project.json`; do not read environment-variable configuration.
- Do not persist or print API keys, prompts, raw LLM responses, exception messages, headers, URLs with queries, Ground Truth, Oracle fields, raw CLS logs, or private chain-of-thought.
- Agent, Planner, RAG, tool capabilities, Decision, Validator, and reports must not read `ground_truth.yaml` or evaluator-only semantic rubrics.
- Do not modify MCP call arguments, Snapshot registered calls, tool fingerprints, Benchmark weights, pass thresholds, recovery policy, historical Archive artifacts, or the six-step/two-Replan budgets.
- A causal intent is investigation metadata, not evidence. It must not support a hypothesis without a persisted Evidence ID and a supporting Observation.
- Missing or ambiguous trigger remains fail-closed; do not fall back to a Public Hypothesis description or arbitrary mechanism.
- Do not run the full pytest suite. Run only the focused nodes and the affected regression groups listed in Task 5.
- Run a real APY-013 at most once after every offline gate passes; do not retry the full Benchmark.
- Implementation is performed inline by the primary Agent. The only subagent is the single read-only plan reviewer required before implementation.

---

## File Structure

- Create `apps/backend/src/super_ai/aiops/causal_intents.py`: tool capability registry, minimal plan coverage assignment, supported-hypothesis causal coverage, and missing-role plan selection.
- Modify `apps/backend/src/super_ai/aiops/reasoning.py`: require and validate `DiagnosticPlanStep.causal_intent` against caller-supplied capabilities.
- Modify `apps/backend/src/super_ai/aiops/diagnostics.py`: expose capabilities to Planner/Replanner, repair plan coverage, normalize Observation roles, route Sufficiency by missing roles, and call structured Decision generation.
- Modify `apps/backend/src/super_ai/aiops/decision_validation.py`: generalize the existing structured invoker and add a bounded structured Root Cause Decision outcome.
- Modify `apps/backend/src/super_ai/evaluation/artifacts.py`: retain allowlisted Observation role-origin audit fields without changing scoring.
- Create `apps/backend/tests/test_aiops_causal_intents.py`: pure capability, coverage assignment, ambiguity, and answer-isolation tests.
- Modify `apps/backend/tests/test_aiops_reasoning_trace.py`: Plan parser, LangGraph routes, role correction, Replanner, Decision and APY-013 workflow regression.
- Modify `apps/backend/tests/test_aiops_decision_validation.py`: structured Decision success, format correction, safe model failure, and retry exhaustion tests.
- Modify `apps/backend/tests/test_evaluation_artifacts.py`: allowlisted causal-role audit extraction and absence of private fields.
- Modify `apps/backend/tests/test_live_diagnostic_adapter.py`: generic Live Plan causal intents and unchanged MCP argument contracts.
- Modify `docs/aiops/agentpy-domainbench.md`: record the offline gates and the one real APY-013 result.

### Task 1: Typed causal-intent capability and plan coverage

**Files:**
- Create: `apps/backend/src/super_ai/aiops/causal_intents.py`
- Modify: `apps/backend/src/super_ai/aiops/reasoning.py`
- Create: `apps/backend/tests/test_aiops_causal_intents.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `CausalIntent = Literal["trigger", "mechanism", "impact", "context"]`.
- Produces: `allowed_causal_intents(tool_name: str) -> frozenset[CausalIntent]`.
- Produces: `PlanCausalCoverage` and `repair_plan_causal_coverage(steps: Sequence[DiagnosticPlanStep]) -> PlanCausalCoverage`.
- Extends: `DiagnosticPlanStep.causal_intent` and `DiagnosticPlanStep.causal_intent_origin`.
- Extends: `parse_plan(..., causal_capabilities: Mapping[str, Collection[CausalIntent]])`.

- [ ] **Step 1: Write failing Plan parser and capability tests**

Create `tests/test_aiops_causal_intents.py` with direct tests for the registry and coverage repair. Add parser tests to `test_aiops_reasoning_trace.py`:

```python
def test_plan_requires_causal_intent() -> None:
    with pytest.raises(ValueError, match="causalIntent"):
        parse_plan(
            '{"steps":[{"id":"x","tool":"InspectPostgresWaitGraph",'
            '"arguments":{},"purpose":"inspect","testsHypotheses":["deadlock"]}]}',
            available_tools={"InspectPostgresWaitGraph"},
            known_hypotheses={"deadlock"},
            causal_capabilities={"InspectPostgresWaitGraph": {"mechanism"}},
        )


def test_plan_rejects_intent_outside_tool_capability() -> None:
    with pytest.raises(ValueError, match="causalIntent"):
        parse_plan(
            '{"steps":[{"id":"x","tool":"InspectPostgresWaitGraph",'
            '"arguments":{},"purpose":"inspect","testsHypotheses":["deadlock"],'
            '"causalIntent":"trigger"}]}',
            available_tools={"InspectPostgresWaitGraph"},
            known_hypotheses={"deadlock"},
            causal_capabilities={"InspectPostgresWaitGraph": {"mechanism"}},
        )


def test_plan_coverage_minimally_repairs_all_mechanism_plan() -> None:
    steps = (
        DiagnosticPlanStep("errors", "InspectPostgresErrors", {}, "errors", ("deadlock",), "mechanism"),
        DiagnosticPlanStep("graph", "InspectPostgresWaitGraph", {}, "graph", ("deadlock",), "mechanism"),
        DiagnosticPlanStep("order", "InspectTransactionResourceOrder", {}, "order", ("deadlock",), "mechanism"),
    )

    result = repair_plan_causal_coverage(steps)

    assert result.complete is True
    assert [item.causal_intent for item in result.steps] == ["impact", "mechanism", "trigger"]
    assert [item.causal_intent_origin for item in result.steps] == [
        "coverage_repair", "model", "coverage_repair"
    ]


def test_plan_coverage_does_not_claim_completion_without_capable_tools() -> None:
    result = repair_plan_causal_coverage(
        (DiagnosticPlanStep("metrics", "GetDatabaseMetrics", {}, "metrics", (), "context"),)
    )

    assert result.complete is False
    assert result.missing_roles == ("trigger", "mechanism", "impact")
```

Also assert that `allowed_causal_intents` source and `repr` do not contain `APY-`, `ground_truth`, `primary_cause`, `oracle`, or canonical root-cause labels.

- [ ] **Step 2: Run the new nodes and verify RED**

Run:

```powershell
uv run pytest tests/test_aiops_causal_intents.py tests/test_aiops_reasoning_trace.py::test_plan_requires_causal_intent tests/test_aiops_reasoning_trace.py::test_plan_rejects_intent_outside_tool_capability -q
```

Expected: collection/import failure because the module, fields, and parser argument do not exist.

- [ ] **Step 3: Implement the typed registry and bounded minimum-change assignment**

In `reasoning.py`, extend the dataclass without defaults so new model plans cannot silently become context:

```python
CausalIntent = CausalRole
CausalIntentOrigin = Literal["model", "coverage_repair", "generic"]

@dataclass(frozen=True, slots=True)
class DiagnosticPlanStep:
    id: str
    tool: str
    arguments: dict[str, object]
    purpose: str
    tests_hypotheses: tuple[str, ...]
    causal_intent: CausalIntent
    causal_intent_origin: CausalIntentOrigin = "model"
```

Make `parse_plan` require `causalIntent`, check it against the four-value allowlist, and reject it unless it belongs to `causal_capabilities[tool]`.

In `causal_intents.py`, define explicit general-purpose sets. The initial registry must cover every current diagnostic tool from `evaluation.artifacts`, including these required entries:

```python
_TRIGGER_OR_MECHANISM = frozenset({
    "GetDeploymentChanges", "InspectClientRetryPolicy", "InspectHttpAttempts",
    "InspectRateLimitTimeline", "InspectTransactionResourceOrder",
})
_MECHANISM = frozenset({
    "InspectPostgresWaitGraph",
})
_MECHANISM_OR_IMPACT = frozenset({
    "InspectGatewayErrors", "InspectPostgresErrors",
})
_CONTEXT_OR_MECHANISM = frozenset({
    "GetDatabaseMetrics", "GetGatewayMetrics", "GetRedisConnectionMetrics",
    "GetServiceMetrics", "GetServiceTopology", "InspectContainer",
    "InspectDatabasePool", "InspectGatewayRequestTimeline", "InspectHostLimits",
    "InspectNginx", "InspectPostgres", "InspectPostgresSessions", "InspectRedis",
    "InspectRedisClientPool", "InspectRedisServer", "InspectTrafficAndDependencyHealth",
    "ListRedisClients", "ProbeUpstreamHealth", "QueryMetrics", "QueryTrace",
})
_TRIGGER_OR_MECHANISM |= frozenset({"InspectPostgresLockGraph"})
_CONTEXT_OR_IMPACT = frozenset({"VerifyServiceHealth"})
_MECHANISM_OR_IMPACT |= frozenset({"InspectPostgresSessions"})
_ANY_DIAGNOSTIC_ROLE = frozenset({"SearchLog", "SearchLogs"})
```

Unknown tools return `frozenset({"context"})`. Knowledge, write-capable recovery and recovery-proposal tools return an empty set. Read-only health verification remains `context | impact` so the existing Docker Live generic Plan stays compatible.

Implement plan repair by enumerating the product of each step's at-most-four allowed roles. Keep assignments with exactly one trigger, at least one mechanism and at least one impact; minimize changed intents, then select the lexicographically stable role tuple using priority `trigger, mechanism, impact, context`. Because plans are capped at six steps, this remains bounded at `4**6` combinations.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```powershell
uv run pytest tests/test_aiops_causal_intents.py tests/test_aiops_reasoning_trace.py::test_plan_requires_causal_intent tests/test_aiops_reasoning_trace.py::test_plan_rejects_intent_outside_tool_capability -q
uv run ruff check src/super_ai/aiops/causal_intents.py src/super_ai/aiops/reasoning.py tests/test_aiops_causal_intents.py
```

Expected: PASS and Ruff clean.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- apps/backend/src/super_ai/aiops/causal_intents.py apps/backend/src/super_ai/aiops/reasoning.py apps/backend/tests/test_aiops_causal_intents.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "feat: add causal intent plan contract"
```

### Task 2: Planner, Replanner and unchanged Tool Calling integration

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Modify: `apps/backend/tests/test_live_diagnostic_adapter.py`

**Interfaces:**
- Consumes: `allowed_causal_intents`, `repair_plan_causal_coverage`, and typed `DiagnosticPlanStep` from Task 1.
- Produces: Planner/Replanner payload fields `causalIntent`, `causalIntentOrigin`, `planCausalCoverageComplete`, and `missingCausalRoles`.
- Preserves: `_step_fingerprint` based only on tool name and normalized arguments.

- [ ] **Step 1: Write failing Planner and fingerprint tests**

Update scripted Planner responses to include `causalIntent`. Add a model response with errors/wait/order all set to mechanism and assert `_create_plan` returns:

```python
assert [(step["tool"], step["causalIntent"]) for step in plan] == [
    ("InspectPostgresErrors", "impact"),
    ("InspectPostgresWaitGraph", "mechanism"),
    ("InspectTransactionResourceOrder", "trigger"),
]
assert plan[0]["causalIntentOrigin"] == "coverage_repair"
assert plan[1]["causalIntentOrigin"] == "model"
assert plan[2]["causalIntentOrigin"] == "coverage_repair"
```

Add a regression proving two otherwise identical steps with different causal intents have the same `_step_fingerprint`. In `test_live_diagnostic_adapter.py`, assert every generic Plan step has `causalIntent` and the existing arguments are byte-for-byte equal to the previous expected dictionaries.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py tests/test_live_diagnostic_adapter.py -k "causal_intent or fingerprint or generic_fallback or model_plan_arguments" -q
```

Expected: failures because payloads and prompts lack causal-intent fields.

- [ ] **Step 3: Integrate capabilities into plan creation**

Change `_tool_contracts_payload` to add only a local wrapper field:

```python
{
    "name": item.name,
    "description": item.description,
    "inputSchema": item.input_schema,
    "allowedCausalIntents": sorted(allowed_causal_intents(item.name)),
}
```

Update Planner and Replanner prompts to require `causalIntent`. Pass a capability mapping into `parse_plan`, convert typed steps with `_diagnostic_plan_step_payload`, and then call `repair_plan_causal_coverage` before argument normalization. `_diagnostic_plan_step_payload` must include intent and origin.

Do not change `normalize_tool_plan_steps`, `tool_step_fingerprint`, MCP schemas or registered arguments. Add coverage status to the Planner and Replanner step payload, not to MCP calls.

- [ ] **Step 4: Make Replanner accept only missing-role steps**

Read `missingCausalRoles` from `state.evidence_sufficiency`. When non-empty, accept a new step only if its `causalIntent` belongs to that set. Continue rejecting executed fingerprints and enforcing remaining budget. Persist the rejected count only as `causalIntentRejectedStepCount`; do not save rejected model text.

- [ ] **Step 5: Run Task 2 tests and verify GREEN**

Run:

```powershell
uv run pytest tests/test_aiops_causal_intents.py tests/test_aiops_reasoning_trace.py -k "plan or causal_intent or fingerprint or replan" tests/test_live_diagnostic_adapter.py -q
uv run ruff check src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py tests/test_live_diagnostic_adapter.py
```

Expected: PASS and Ruff clean.

- [ ] **Step 6: Commit Task 2**

```powershell
git add -- apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_live_diagnostic_adapter.py
git commit -m "feat: enforce causal coverage before tool execution"
```

### Task 3: Observation normalization and deterministic Sufficiency routing

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/causal_intents.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Test: `apps/backend/tests/test_aiops_causal_intents.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Test: `apps/backend/tests/test_evaluation_artifacts.py`

**Interfaces:**
- Produces: `CausalCoverage` and `supported_causal_coverage(...) -> CausalCoverage`.
- Produces: `next_causal_refinement_index(...) -> int | None`.
- Extends: `ObservationDecision` with optional `causal_role_origin`, `reported_causal_role`, and `causal_role_corrected` audit fields whose parser defaults do not alter model semantics.
- Persists: `causalRoleOrigin`, `reportedCausalRole`, `causalRoleCorrected`, `missingCausalRoles`, and `ambiguousTrigger`.

- [ ] **Step 1: Write failing role-correction and coverage-route tests**

Add a scripted Evidence Evaluator that returns `mechanism` for a Plan step whose validated intent is trigger. Assert the persisted Observation retains the model summary/support/evidence but has:

```python
assert observation["causalRole"] == "trigger"
assert observation["causalRoleOrigin"] == "plan_contract"
assert observation["reportedCausalRole"] == "mechanism"
assert observation["causalRoleCorrected"] is True
```

Add pure coverage tests: only Evidence-linked Observations supporting the unique supported hypothesis count; one trigger, one mechanism and one impact is complete; two triggers set `ambiguous_trigger=True`.

Add Sufficiency tests for these routes:

```python
# LLM says sufficient, trigger missing, matching unexecuted Plan step exists
assert update["next_route"] == "executor"
assert update["plan_index"] == trigger_step_index
assert gate.payload["missingCausalRoles"] == ["trigger"]

# no matching Plan step, budget remains
assert update["next_route"] == "replanner"

# complete coverage
assert update["next_route"] == "decision"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
uv run pytest tests/test_aiops_causal_intents.py tests/test_aiops_reasoning_trace.py -k "causal_role or causal_coverage or sufficiency" tests/test_evaluation_artifacts.py -q
```

Expected: failures because correction audit fields and deterministic routing are absent.

- [ ] **Step 3: Implement public causal coverage**

Add:

```python
@dataclass(frozen=True, slots=True)
class CausalCoverage:
    trigger_count: int
    mechanism_count: int
    impact_count: int
    missing_roles: tuple[Literal["trigger", "mechanism", "impact"], ...]
    ambiguous_trigger: bool

    @property
    def complete(self) -> bool:
        return (
            self.trigger_count == 1
            and self.mechanism_count >= 1
            and self.impact_count >= 1
        )
```

`supported_causal_coverage` must first require exactly one supported hypothesis, then count only non-empty summaries whose `supports` contains that hypothesis and whose Evidence IDs intersect its state Evidence IDs.

- [ ] **Step 4: Normalize Observation role to the validated Plan contract**

In `_evidence_evaluator`, read `current_plan_step.causalIntent`. After successful parsing, preserve the allowlisted reported role and replace only `ObservationDecision.causal_role` when it differs. On model/schema failure, retain the existing empty support/refute fallback but set its causal role from the Plan. Extend `_observation_decision_payload` with the three safe audit fields. Never parse or persist the raw response to obtain an invalid role.

Extend the shared dataclass with trailing defaults so existing constructors remain compatible:

```python
causal_role_origin: Literal["model", "plan_contract"] | None = None
reported_causal_role: CausalRole | None = None
causal_role_corrected: bool = False
```

Update Artifact extraction to preserve the allowlisted role origin and correction boolean, with missing historical fields defaulting to `None/False`.

- [ ] **Step 5: Override LLM Sufficiency only for causal coverage**

After parsing the existing Sufficiency Decision, calculate deterministic coverage. If its status is sufficient but coverage is incomplete, first select an unexecuted Plan step whose intent is missing and whose `testsHypotheses` includes the supported hypothesis. Otherwise route to Replanner while budget remains. Persist coverage counts, `missingCausalRoles` and `ambiguousTrigger`. Complete coverage does not override an LLM `insufficient` evidence decision.

- [ ] **Step 6: Run Task 3 tests and verify GREEN**

Run:

```powershell
uv run pytest tests/test_aiops_causal_intents.py tests/test_aiops_reasoning_trace.py -k "causal_role or causal_coverage or sufficiency" tests/test_evaluation_artifacts.py -q
uv run ruff check src/super_ai/aiops/causal_intents.py src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/artifacts.py tests/test_aiops_causal_intents.py tests/test_aiops_reasoning_trace.py tests/test_evaluation_artifacts.py
```

Expected: PASS and Ruff clean.

- [ ] **Step 7: Commit Task 3**

```powershell
git add -- apps/backend/src/super_ai/aiops/causal_intents.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/tests/test_aiops_causal_intents.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_evaluation_artifacts.py
git commit -m "feat: route diagnostics by causal evidence coverage"
```

### Task 4: Bounded structured Root Cause Decision generation

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Test: `apps/backend/tests/test_aiops_decision_validation.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `StructuredDecisionOutcome`.
- Produces: `invoke_structured_root_cause_decision(model: ChatModel, prompt: str, available_evidence_ids: Set[str]) -> StructuredDecisionOutcome`.
- Generalizes: `_structured_invoker(model: ChatModel, schema: type[BaseModel]) -> _AsyncInvoker | None`.
- Persists: `decisionAttempts`, `decisionErrorCodes`, `decisionErrorCode`, `decisionErrorPhase`, `decisionRetryable`, and `decisionHttpStatusClass`.

- [ ] **Step 1: Write failing structured Decision tests**

In `test_aiops_decision_validation.py`, mirror the existing Validator envelope fakes and assert:

```python
outcome = await invoke_structured_root_cause_decision(
    model=StructuredDecisionModel(parsed_payload),
    prompt="public decision prompt",
    available_evidence_ids={"ev-trigger", "ev-mechanism", "ev-impact"},
)
assert outcome.decision is not None
assert outcome.attempts == 1
assert outcome.error_category is None
```

Add tests for: first parse error followed by success on attempt two; two parse errors produce `retry_exhausted` and `("invalid_json_or_schema",)`; `APITimeoutError` produces only `timeout/model_invoke/retryable=True`; unsupported structured setup returns the existing safe `structured_output_unsupported/structured_invoker_setup/retryable=False` outcome rather than silently changing provider mode. Assert exception messages and raw payloads never appear in `repr(outcome)`.

- [ ] **Step 2: Run structured Decision tests and verify RED**

Run:

```powershell
uv run pytest tests/test_aiops_decision_validation.py -k "structured_decision" -q
```

Expected: import failure because the outcome and invocation function do not exist.

- [ ] **Step 3: Generalize the existing structured invoker**

Change the private helper to accept a Pydantic schema while preserving Validator behavior:

```python
def _structured_invoker(
    model: ChatModel,
    schema: type[BaseModel],
) -> _AsyncInvoker | None:
    method_value = getattr(model, "with_structured_output", None)
    if not callable(method_value):
        return None
    return cast(
        _AsyncInvoker,
        method_value(schema, method="function_calling", include_raw=True),
    )
```

Keep `invoke_structured_root_cause_validation` behavior identical by passing `_RootCauseValidationSchema`.

- [ ] **Step 4: Implement the Decision schema and bounded invocation**

Add a private Pydantic schema with `extra="forbid"`, aliases, `causal_chain: list[str] = Field(alias="causalChain", min_length=2, max_length=6)`, non-empty evidence IDs, and confidence bounds. The invocation loop is exactly attempts `(1, 2)`: classify model exceptions with the existing safe classifier, append a fixed correction suffix only after schema failure, and parse through `parse_root_cause_decision` so Evidence ID ownership remains enforced.

- [ ] **Step 5: Integrate the outcome into the Decision node**

Replace the raw `ainvoke` block with `invoke_structured_root_cause_decision`. Preserve label normalization, `_repair_grounded_causal_chain`, and `build_grounded_fallback_decision`. Persist only allowlisted outcome fields. If the LLM result fails but grounded fallback succeeds, retain both the fallback origin and the safe model error audit. Do not automatically retry the graph or Benchmark.

- [ ] **Step 6: Run Task 4 tests and verify GREEN**

Run:

```powershell
uv run pytest tests/test_aiops_decision_validation.py tests/test_aiops_reasoning_trace.py -k "structured_decision or decision_node or grounded" -q
uv run ruff check src/super_ai/aiops/decision_validation.py src/super_ai/aiops/diagnostics.py tests/test_aiops_decision_validation.py tests/test_aiops_reasoning_trace.py
```

Expected: PASS and existing Validator tests remain green.

- [ ] **Step 7: Commit Task 4**

```powershell
git add -- apps/backend/src/super_ai/aiops/decision_validation.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_decision_validation.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "fix: generate root cause decisions with structured output"
```

### Task 5: Scoped regression, one real APY-013 and acceptance record

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Runtime only: `apps/backend/var/benchmarks/APY-013-causal-intent-routing-real.json`
- External configured Archive only: one terminal run artifact.

**Interfaces:**
- Consumes: Tasks 1-4.
- Produces: verified offline gates and one auditable real APY-013 result.

- [ ] **Step 1: Run the affected tests in two bounded groups**

Run group A:

```powershell
uv run pytest tests/test_aiops_causal_intents.py tests/test_evaluation_scoring.py tests/test_evaluation_artifacts.py tests/test_evaluation_scenarios.py tests/test_live_evaluation_scoring.py tests/test_live_evaluation_scenarios.py -q
```

Run group B:

```powershell
uv run pytest tests/test_aiops_decision_validation.py tests/test_aiops_reasoning_trace.py tests/test_evaluation_cli.py tests/test_snapshot_evaluation_tools.py tests/test_knowledge_candidate_safety.py tests/test_live_diagnostic_adapter.py -q
```

Expected: both groups PASS. Do not replace these commands with full pytest.

- [ ] **Step 2: Run static and focused specification gates**

```powershell
uv run ruff check src tests
uv run pyright
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate harden-aiops-decision-validation --strict
```

Expected: Ruff clean, Pyright `0 errors`, focused OpenSpec valid. The known unrelated global OpenSpec warnings are not changed in this task.

- [ ] **Step 3: Audit the one-real-run preconditions**

Run the existing read-only 30-card count query, then:

```powershell
uv run pytest tests/test_live_llm.py -m live_llm -q
uv run python scripts/manage_evaluation_history.py audit --config ../../config/project.json
```

Expected: exactly 30 `ready/indexed` documents, LLM readiness PASS, and no Archive checksum conflict. Do not print user configuration or credentials.

- [ ] **Step 4: Run APY-013 exactly once**

First assert the output path does not exist. Then run only once:

```powershell
uv run python scripts/run_snapshot_benchmark.py --scenario APY-013 --suite-version evidence-v4 --runs 1 --adapter application --config ../../config/project.json --rag-mode on --owner-user-id user_c88807ff36b74a038b9e1ea31a389cfc --knowledge-base-id kb_user_c88807ff36b74a038b9e1ea31a389cfc --output var/benchmarks/APY-013-causal-intent-routing-real.json
```

Use short process polling. If the command wrapper times out, do not rerun; inspect the target JSON, PostgreSQL terminal state and Archive audit to determine whether the child completed.

- [ ] **Step 5: Audit only safe result fields**

Record run ID, duration, dimensions, total, validity, passed, score reason codes, causal-role counts/origins, Decision error allowlist fields, Validator origin/error allowlist fields, recovery mode, policy authorization and `executionPermitted`. Do not print summaries, raw Evidence, Prompt, raw response or exception text.

Acceptance requires unique trigger, mechanism and impact; a Root Cause Decision; `observations_evaluated=5/5`; no Ground Truth leakage; and no unauthorized recovery. A failed run must be documented as failed rather than retried or relabeled.

- [ ] **Step 6: Update documentation and verify repository hygiene**

Append the implementation commits, offline gate results and one real run outcome to `docs/aiops/agentpy-domainbench.md`. Then run:

```powershell
git diff --check
git status --short
uv run python scripts/manage_evaluation_history.py audit --config ../../config/project.json
uv run python scripts/manage_evaluation_history.py summarize --config ../../config/project.json
```

Expected: no tracked `var/`, Archive, secret configuration or raw logs; zero checksum conflict and zero database pending.

- [ ] **Step 7: Commit the acceptance record**

```powershell
git add -- docs/aiops/agentpy-domainbench.md
git commit -m "docs: record causal intent routing acceptance"
```

## Final Verification

- [ ] Re-run Task 5 Steps 1 and 2 after the documentation commit; do not run full pytest.
- [ ] Run `git status --short` and confirm the worktree is clean.
- [ ] Run Archive `audit` and `summarize`; confirm no pending/conflict.
- [ ] Do not run APY-013 again; final reporting must reference the single run from Task 5 Step 4.
