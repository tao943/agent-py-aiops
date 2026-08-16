# Resilient Decision Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the AIOps Decision Validator so provider/format failures cannot masquerade as evidence gaps, while a strictly evidence-grounded candidate can survive as an audited, recovery-restricted deterministic fallback.

**Architecture:** Add a focused domain module that performs deterministic grounded validation and a focused async adapter that wraps the existing LangChain chat model for structured validation with one format-only correction attempt. Integrate both into the existing LangGraph node, persist safe validation provenance in JSONB payloads, prevent unavailable validators from entering the Replanner, and force deterministic fallbacks through manual-review recovery.

**Tech Stack:** Python 3.10, Pydantic v2, LangChain 1.3, LangGraph 1.2, `langchain-openai`, PostgreSQL JSONB, pytest/pytest-asyncio, Ruff, strict Pyright, OpenSpec CLI.

## Global Constraints

- Do not read or indirectly use `ground_truth.yaml`, scoring answers, or hidden evaluator milestones.
- RAG knowledge references guide investigation but never count as positive incident evidence.
- Use only public hypotheses, public decision vocabulary, task-scoped persisted Evidence IDs, and Evidence-linked Observation decisions.
- Validator unavailability must not become `missingEvidence`, trigger unrelated tools, or repeat evidence collection.
- A deterministic fallback cannot authorize automatic recovery; it must require manual review or external policy.
- Do not change Ground Truth, benchmark weights, thresholds, or canonical answers.
- Store new audit data only in existing step/checkpoint JSONB payloads; add no database migration.
- Add no dependency, external service, frontend field, or HTTP/SSE contract.
- Preserve Python 3.10 compatibility, Ruff, strict Pyright, and the existing LangGraph topology.
- Never persist complete model responses, prompts, stack traces, credentials, or raw sensitive logs.
- Write the new validation-origin contract as `workflowVersion=evidence-driven-v3`; keep historical v2 Artifact behavior readable and scoreable.

---

## File Map

- Create `openspec/changes/harden-aiops-decision-validation/`: proposal, design, tasks, and delta specs for `aiops-diagnosis-tasks` and `agentpy-sre-benchmark`.
- Create `apps/backend/src/super_ai/aiops/decision_validation.py`: deterministic evidence contract, safe validation outcome types, Pydantic structured schema, and bounded validator invocation.
- Create `apps/backend/tests/test_aiops_decision_validation.py`: focused unit tests for all deterministic checks and model failure classifications.
- Modify `apps/backend/src/super_ai/aiops/diagnostics.py`: call the new validator, route only explicit evidence gaps to Replanner, persist audit provenance, and restrict fallback recovery.
- Modify `apps/backend/tests/test_aiops_reasoning_trace.py`: workflow integration and APY-013 regression tests.
- Modify `apps/backend/src/super_ai/evaluation/artifacts.py`: require an allowlisted valid validation origin for evidence-driven-v3 artifacts while retaining historical v2 behavior.
- Modify `apps/backend/tests/test_evaluation_artifacts.py`: artifact provenance contract tests.
- Modify `docs/aiops/agentpy-domainbench.md`: document validation origins, failure semantics, and real acceptance evidence.

---

### Task 1: Declare the OpenSpec behavior before production code

**Files:**
- Create: `openspec/changes/harden-aiops-decision-validation/proposal.md`
- Create: `openspec/changes/harden-aiops-decision-validation/design.md`
- Create: `openspec/changes/harden-aiops-decision-validation/tasks.md`
- Create: `openspec/changes/harden-aiops-decision-validation/specs/aiops-diagnosis-tasks/spec.md`
- Create: `openspec/changes/harden-aiops-decision-validation/specs/agentpy-sre-benchmark/spec.md`

**Interfaces:**
- Consumes: confirmed design in `docs/superpowers/specs/2026-08-16-resilient-decision-validation-design.md`.
- Produces: normative scenarios for validation origin, unavailable-validator routing, fallback recovery restrictions, and artifact eligibility.

- [ ] **Step 1: Create the focused change skeleton**

Run:

```powershell
openspec new change harden-aiops-decision-validation
```

Expected: `openspec/changes/harden-aiops-decision-validation/` exists and no other active change is modified.

- [ ] **Step 2: Write proposal and design**

`proposal.md` must declare:

```markdown
## Why

真实 APY-013 已生成正确且有证据支持的根因，但 LLM Validator 不可用被误判为证据缺口，导致无关 Replan、重复模型调用和根因丢失。

## What Changes

- 将 Validator 的模型调用失败、格式失败和明确证据拒绝分开审计。
- 增加严格的公开证据确定性验证，并只在该验证完整通过时允许降级结论。
- 禁止 Validator 基础设施故障进入证据 Replanner。
- 降级结论只能进入人工审批或外部策略恢复路径。

## Capabilities

### Modified Capabilities

- `aiops-diagnosis-tasks`
- `agentpy-sre-benchmark`
```

`design.md` must copy the architecture, nine deterministic checks, error categories, JSONB audit fields, and recovery restriction from the confirmed design without adding new scope.

- [ ] **Step 3: Write delta requirements and scenarios**

The `aiops-diagnosis-tasks` delta must include these scenarios:

```markdown
## ADDED Requirements

### Requirement: Decision validation failures remain distinct from evidence gaps

#### Scenario: Validator provider failure after grounded checks pass
- **WHEN** the candidate passes every public deterministic evidence check and the LLM Validator call fails
- **THEN** the workflow SHALL preserve the candidate with `validationOrigin=deterministic_grounded_fallback`
- **AND** it SHALL NOT route to the evidence Replanner
- **AND** its recovery policy SHALL require manual review or external policy

#### Scenario: Validator explicitly rejects supported fields
- **WHEN** the LLM Validator returns a valid structured `invalid` result with specific unsupported fields or missing evidence
- **THEN** the workflow SHALL classify it as `model_rejected`
- **AND** only a bounded gap-targeted Replan MAY run

#### Scenario: Deterministic evidence contract fails
- **WHEN** the candidate lacks unique support, public labels, task evidence, independent observations, or a grounded causal chain
- **THEN** the workflow SHALL fail closed and SHALL NOT use deterministic fallback
```

The `agentpy-sre-benchmark` delta must require evidence-driven-v3 artifacts to accept only `llm_confirmed` or `deterministic_grounded_fallback`, with `status=valid`, retain historical v2 `status=valid` behavior, and prohibit Ground Truth from validator inputs.

- [ ] **Step 4: Validate the specification**

Run:

```powershell
openspec validate harden-aiops-decision-validation --strict
```

Expected: change is valid with zero errors.

- [ ] **Step 5: Commit the specification**

```powershell
git add openspec/changes/harden-aiops-decision-validation
git commit -m "docs: specify resilient decision validation"
```

---

### Task 2: Build the deterministic grounded-evidence contract

**Files:**
- Create: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Create: `apps/backend/tests/test_aiops_decision_validation.py`

**Interfaces:**
- Consumes: `RootCauseDecision`, task-scoped `available_evidence_ids`, `hypothesis_states`, `observation_decisions`, and public `decision_vocabulary`.
- Produces:
  - `DecisionValidationErrorCategory = Literal["candidate_missing", "deterministic_gap", "model_call_failed", "invalid_model_output", "model_rejected", "retry_exhausted"]`
  - `DeterministicValidationResult(passed, supported_hypothesis_id, checks, unsupported_fields, missing_evidence)`
  - `validate_grounded_candidate(*, candidate: RootCauseDecision, available_evidence_ids: Set[str], hypothesis_states: Sequence[JsonDict], observation_decisions: Sequence[JsonDict], decision_vocabulary: Mapping[str, object]) -> DeterministicValidationResult`
  - `deterministic_checks_payload(result: DeterministicValidationResult) -> list[JsonDict]`

- [ ] **Step 1: Write failing tests for the fully grounded case**

Create a fixture with one supported `postgres_deadlock`, two refuted competitors, three task Evidence IDs, three supporting Observation summaries, and exact vocabulary labels. Assert:

```python
result = validate_grounded_candidate(
    candidate=deadlock_decision,
    available_evidence_ids={"ev-error", "ev-cycle", "ev-order"},
    hypothesis_states=deadlock_states,
    observation_decisions=deadlock_observations,
    decision_vocabulary=deadlock_vocabulary,
)

assert result.passed is True
assert result.supported_hypothesis_id == "postgres_deadlock"
assert result.unsupported_fields == ()
assert result.missing_evidence == ()
assert all(check.passed for check in result.checks)
```

- [ ] **Step 2: Run the focused test and confirm red**

Run:

```powershell
cd apps/backend
./.venv/Scripts/python.exe -m pytest tests/test_aiops_decision_validation.py -q
```

Expected: collection fails because `super_ai.aiops.decision_validation` does not exist.

- [ ] **Step 3: Add domain types and the minimal passing validator**

Implement frozen slot dataclasses and allowlisted check codes:

```python
DecisionValidationErrorCategory = Literal[
    "candidate_missing",
    "deterministic_gap",
    "model_call_failed",
    "invalid_model_output",
    "model_rejected",
    "retry_exhausted",
]

@dataclass(frozen=True, slots=True)
class DeterministicCheck:
    code: Literal[
        "unique_supported_hypothesis",
        "no_open_competitor",
        "public_label_match",
        "task_evidence_only",
        "supporting_evidence_only",
        "independent_positive_evidence",
        "supporting_observations",
        "grounded_causal_chain",
        "trigger_present",
        "confidence_in_range",
    ]
    passed: bool

@dataclass(frozen=True, slots=True)
class DeterministicValidationResult:
    passed: bool
    supported_hypothesis_id: str | None
    checks: tuple[DeterministicCheck, ...]
    unsupported_fields: tuple[Literal["component", "mechanism", "trigger", "causalChain"], ...]
    missing_evidence: tuple[str, ...]
```

The validator must derive positive Evidence IDs only from Observation entries whose `supports` contains the unique supported hypothesis. It must require at least two distinct linked IDs, require candidate IDs to be both a subset of `available_evidence_ids` and a subset of those positive Observation Evidence IDs, and require every causal-chain entry to equal one linked Observation summary. A candidate containing an Alert, Knowledge reference, foreign-task, or otherwise non-supporting Evidence ID must fail even when two other supporting Observations exist.

- [ ] **Step 4: Add one failing test per forbidden fallback condition**

Use `pytest.mark.parametrize` for:

```python
(
    "multiple_supported",
    "open_competitor",
    "wrong_public_label",
    "foreign_evidence_id",
    "available_but_non_supporting_evidence_id",
    "one_positive_evidence",
    "one_supporting_observation",
    "invented_causal_step",
    "blank_trigger",
    "confidence_out_of_range",
)
```

Each mutation must assert `result.passed is False` and the exact failed check code. Add a separate test proving knowledge-like IDs absent from supporting Observations cannot satisfy independent evidence.

Add another regression where two supporting Observations exist but the candidate cites only an Alert-like ID that is also present in `available_evidence_ids`; assert `supporting_evidence_only` fails. This prevents unrelated available IDs from piggybacking on separate supporting Observations.

- [ ] **Step 5: Run and make all deterministic tests green**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_aiops_decision_validation.py -q
```

Expected: all deterministic validation tests pass.

- [ ] **Step 6: Commit the deterministic contract**

```powershell
git add src/super_ai/aiops/decision_validation.py tests/test_aiops_decision_validation.py
git commit -m "feat: validate grounded decisions deterministically"
```

---

### Task 3: Add bounded LangChain structured validation and safe failure classification

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Modify: `apps/backend/tests/test_aiops_decision_validation.py`

**Interfaces:**
- Consumes: the existing `ChatModel` returned by `LlmProvider.create_chat_model()`, a validation prompt, and task-scoped Evidence IDs.
- Produces:
  - `StructuredValidationOutcome(decision, error_category, attempts, error_codes)`
  - `invoke_structured_root_cause_validation(model, prompt, available_evidence_ids) -> StructuredValidationOutcome`

- [ ] **Step 1: Write failing async classification tests**

Add fake models for four paths:

```python
@pytest.mark.asyncio
async def test_structured_validator_classifies_provider_failure_without_retry() -> None:
    model = RaisingChatModel(TimeoutError("provider timeout"))
    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )
    assert outcome.decision is None
    assert outcome.error_category == "model_call_failed"
    assert outcome.attempts == 1

@pytest.mark.asyncio
async def test_structured_validator_reasks_once_after_parse_failure() -> None:
    model = SequenceChatModel(["not-json", VALIDATION_JSON])
    outcome = await invoke_structured_root_cause_validation(
        model=model,
        prompt="validate",
        available_evidence_ids={"ev-1", "ev-2"},
    )
    assert outcome.decision is not None
    assert outcome.error_category is None
    assert outcome.attempts == 2
```

Also assert two invalid responses produce `retry_exhausted`, and a parsed `status=invalid` produces `model_rejected` without being treated as infrastructure failure.

Add a fake model exposing `with_structured_output` whose returned runnable yields the production-style envelope:

```python
{
    "raw": FakeAiMessage(),
    "parsed": _RootCauseValidationSchema(
        status="valid",
        evidenceIds=["ev-1", "ev-2"],
        unsupportedFields=[],
        missingEvidence=[],
        summary="The public observations support every field.",
    ),
    "parsing_error": None,
}
```

Assert this branch is unpacked without calling the raw fallback. Add a two-envelope sequence whose parsed decisions cite `ev-foreign`; it must end as `retry_exhausted` because parsed Evidence IDs must still be a subset of `available_evidence_ids`.

- [ ] **Step 2: Run tests and confirm the missing adapter failure**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_aiops_decision_validation.py -q
```

Expected: new tests fail because the async adapter and outcome type are absent.

- [ ] **Step 3: Implement the Pydantic schema and optional LangChain wrapper**

Add a private strict schema with aliases matching the public JSON contract:

```python
class _RootCauseValidationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    status: Literal["valid", "invalid"]
    evidence_ids: list[str] = Field(alias="evidenceIds")
    unsupported_fields: list[
        Literal["component", "mechanism", "trigger", "causalChain"]
    ] = Field(alias="unsupportedFields")
    missing_evidence: list[str] = Field(alias="missingEvidence")
    summary: str = Field(min_length=1)
```

When the runtime model exposes `with_structured_output`, wrap it using the existing LangChain API:

```python
method = getattr(model, "with_structured_output", None)
structured = method(
    _RootCauseValidationSchema,
    method="function_calling",
    include_raw=True,
) if callable(method) else None
```

When a test double or alternate provider lacks that method, retain the current `ainvoke` + `parse_root_cause_validation` path. Never catch `BaseException`; catch model invocation exceptions separately from parsing/schema exceptions.

- [ ] **Step 4: Implement exactly one format-only corrective attempt**

On the first parse/schema failure, append only safe error codes to a correction prompt:

```text
The previous response did not match the required validation schema.
Return JSON only with status, evidenceIds, unsupportedFields, missingEvidence, and summary.
Schema errors: invalid_json_or_schema.
```

Do not include the raw prior response. Do not retry `model_call_failed`. Return attempts `1` or `2` and an allowlisted `error_codes` tuple.

- [ ] **Step 5: Run focused tests and static checks**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_aiops_decision_validation.py -q
./.venv/Scripts/python.exe -m ruff check src/super_ai/aiops/decision_validation.py tests/test_aiops_decision_validation.py
./.venv/Scripts/pyright.exe src/super_ai/aiops/decision_validation.py tests/test_aiops_decision_validation.py
```

Expected: all commands pass; no raw model output appears in assertions or persisted payload helpers.

- [ ] **Step 6: Commit structured validation**

```powershell
git add src/super_ai/aiops/decision_validation.py tests/test_aiops_decision_validation.py
git commit -m "feat: classify structured validator failures"
```

---

### Task 4: Integrate validation origins, correct routing, and recovery restriction

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Consumes: `validate_grounded_candidate`, `invoke_structured_root_cause_validation`, and payload helpers from Task 2/3.
- Produces: a `decision_validation` payload with `status`, `validationOrigin`, `validationErrorCategory`, `validationAttempts`, `validationWarning`, `deterministicChecks`, `missingEvidence`, `unsupportedFields`, and `nextRoute`.

- [ ] **Step 1: Write the unavailable-validator APY-013 integration test**

Create a fake provider whose planner/evaluator/sufficiency/decision responses are the existing valid APY-013 sequence and whose validator raises `TimeoutError`. Assert:

```python
assert [item.tool_name for item in snapshot.observations] == [
    "InspectPostgresErrors",
    "InspectPostgresWaitGraph",
    "InspectTransactionResourceOrder",
]
assert not any(item.tool_name == "GetDatabaseMetrics" for item in snapshot.observations)
assert len([step for step in steps if step.phase == "decision"]) == 1
assert not any(
    step.phase == "replanner"
    and step.payload.get("reason") == "decision_validation_gap"
    for step in steps
)
validation = next(step for step in steps if step.phase == "decision_validation")
assert validation.payload["status"] == "valid"
assert validation.payload["validationOrigin"] == "deterministic_grounded_fallback"
assert validation.payload["validationErrorCategory"] == "model_call_failed"
assert validation.payload["validationAttempts"] == 1
assert validation.payload["validationWarning"] == "llm_validator_unavailable"
assert completed.result_payload["rootCauseDecision"] is not None
assert completed.result_payload["recoveryPlan"]["mode"] == "manual_review"
assert completed.result_payload["recoveryPolicy"]["executionPermitted"] is False
```

- [ ] **Step 2: Write explicit rejection and insufficient-fallback tests**

Preserve the existing explicit validation-gap test, but require `validationErrorCategory=model_rejected` and a concrete non-empty `missingEvidence`. Add a separate case where the Validator fails and only one supporting Observation exists; assert the final root cause is `None`, no deterministic fallback origin is recorded, and no recovery action is authorized.

- [ ] **Step 3: Run the integration tests and confirm red**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest \
  tests/test_aiops_reasoning_trace.py::test_apy_013_validator_unavailable_uses_grounded_fallback \
  tests/test_aiops_reasoning_trace.py::test_invalid_decision_validation_replans_before_reporting \
  tests/test_aiops_reasoning_trace.py::test_validator_unavailable_without_independent_evidence_fails_closed -q
```

Expected: the unavailable case follows the current invalid/Replanner path and fails.

- [ ] **Step 4: Replace the broad Validator exception branch**

In `_decision_validator`:

1. Parse the candidate.
2. Run `validate_grounded_candidate` before any model call.
3. If the candidate is missing, build an invalid payload with `candidate_missing`, route directly to recovery/no-action, and do not Replan.
4. If deterministic checks fail, build an invalid validation payload with `deterministic_gap`; do not claim validator unavailability.
5. Split deterministic failed checks into replanable evidence gaps (`no_open_competitor`, `independent_positive_evidence`, `supporting_observations`) and non-replanable integrity/shape gaps (`public_label_match`, `task_evidence_only`, `grounded_causal_chain`, `trigger_present`, `confidence_in_range`). A deterministic evidence gap can Replan only when the persisted sufficiency payload contains a non-empty `recommendedTools` list whose tools are discovered and not already executed; otherwise fail closed.
6. If checks pass, call `invoke_structured_root_cause_validation`.
7. For a parsed decision, use `llm_confirmed` or `model_rejected`.
8. For `model_call_failed`, `invalid_model_output`, or `retry_exhausted`, create a valid `RootCauseValidationDecision` from the task-scoped candidate Evidence IDs and set `deterministic_grounded_fallback`.
9. Permit one bounded `model_rejected` Replan when either `missingEvidence` or `unsupportedFields` is non-empty and `_can_replan(state)` is true. Existing fingerprint filtering must still prevent repeated tool + arguments pairs.
10. Write `workflowVersion="evidence-driven-v3"` in the planner and report payloads. Update Workflow tests that assert the current production version; do not rewrite historical Artifact fixtures that intentionally exercise v2 compatibility.

The fallback payload must have empty `missingEvidence` and `unsupportedFields`; infrastructure failure details belong only in `validationErrorCategory` and `validationWarning`.

Add a candidate-missing integration assertion:

```python
assert validation.payload["validationErrorCategory"] == "candidate_missing"
assert validation.payload["nextRoute"] == "recovery_planner"
assert completed.result_payload["rootCauseDecision"] is None
assert completed.result_payload["recoveryPolicy"]["executionPermitted"] is False
assert not any(step.phase == "replanner" for step in steps)
```

Add two deterministic-gap route tests: one with a concrete, discovered, unexecuted `recommendedTools` entry that performs exactly one targeted Replan, and one without such a tool that fails closed without Replan.

- [ ] **Step 5: Force fallback recovery to manual review**

At the start of `_recovery_planner`, inspect:

```python
validation = _json_dict(state.get("decision_validation"))
if validation.get("validationOrigin") == "deterministic_grounded_fallback":
    plan = _fallback_recovery_plan(
        candidate,
        proposal_tools=proposal_tools,
        force_manual_review=True,
    )
```

Refactor `_fallback_recovery_plan` to accept `force_manual_review: bool = False`. When true, the resulting plan must have `mode="manual_review"`, `tool=None`, `humanApprovalRequired=True`, and the Policy Gate must remain `executionPermitted=False`.

- [ ] **Step 6: Run workflow regression tests**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_aiops_reasoning_trace.py -q
```

Expected: all reasoning workflow tests pass, APY-013 invokes exactly three evidence tools, and explicit model rejection retains one bounded gap Replan.

- [ ] **Step 7: Commit Workflow integration**

```powershell
git add src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py
git commit -m "fix: preserve strictly grounded decisions on validator outages"
```

---

### Task 5: Version and bind Benchmark Artifact eligibility to audited validation origin

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/tests/test_evaluation_artifacts.py`

**Interfaces:**
- Consumes: persisted `decision_validation` steps from Task 4.
- Produces: v3 artifact decisions only for `status=valid` plus origin `llm_confirmed` or `deterministic_grounded_fallback`; historical v2 keeps its existing `status=valid` rule.

- [ ] **Step 1: Write failing artifact provenance tests**

Add:

```python
@pytest.mark.parametrize(
    "origin",
    ["llm_confirmed", "deterministic_grounded_fallback"],
)
def test_v2_artifact_accepts_allowlisted_valid_validation_origins(origin: str) -> None:
    artifact = build_run_artifact(
        _benchmark_task(),
        (
            _step(1, "planner", {"workflowVersion": "evidence-driven-v3", "plan": []}),
            _step(2, "decision", _decision_payload()),
            _step(3, "decision_validation", {"status": "valid", "validationOrigin": origin}),
        ),
        (), (), (),
    )
    assert artifact.decision is not None

def test_v2_artifact_rejects_valid_status_with_unknown_origin() -> None:
    artifact = build_run_artifact(
        _benchmark_task(),
        (
            _step(1, "planner", {"workflowVersion": "evidence-driven-v3", "plan": []}),
            _step(2, "decision", _decision_payload()),
            _step(
                3,
                "decision_validation",
                {"status": "valid", "validationOrigin": "unknown"},
            ),
        ),
        (),
        (),
        (),
    )
    assert artifact.decision is None
```

Name the new strict tests `test_v3_*` and set their planner payload to `workflowVersion="evidence-driven-v3"`. Add an explicit historical compatibility test using `workflowVersion="evidence-driven-v2"`, `status="valid"`, and no `validationOrigin`; assert the legacy decision remains scoreable.

- [ ] **Step 2: Run tests and confirm unknown-origin behavior is currently wrong**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_evaluation_artifacts.py -q
```

Expected: v3 unknown origin is currently accepted and the test fails; the v2 compatibility test passes before and after the change.

- [ ] **Step 3: Enforce the provenance allowlist**

In `_decision_from_steps`, detect the latest planner Workflow version. Keep the current v2 status rule and require the origin allowlist only for v3:

```python
validation = validations[-1].payload
if validation.get("status") != "valid":
    return None
if (
    workflow_version == "evidence-driven-v3"
    and validation.get("validationOrigin")
    not in {"llm_confirmed", "deterministic_grounded_fallback"}
):
    return None
```

Do not inspect report prose, Ground Truth, or error messages.

- [ ] **Step 4: Run artifact tests and commit**

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_evaluation_artifacts.py -q
git add src/super_ai/evaluation/artifacts.py tests/test_evaluation_artifacts.py
git commit -m "test: require audited decision validation origins"
```

---

### Task 6: Verify offline behavior, document evidence, and run one paid acceptance

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Modify: `openspec/changes/harden-aiops-decision-validation/tasks.md`
- Runtime output only: `apps/backend/var/benchmarks/APY-013-resilient-validation-real.json`

**Interfaces:**
- Consumes: all implementation tasks.
- Produces: offline verification evidence, one real APY-013 scorecard, and updated operator documentation.

- [ ] **Step 1: Run focused and Snapshot regressions**

From `apps/backend`:

```powershell
./.venv/Scripts/python.exe -m pytest \
  tests/test_aiops_decision_validation.py \
  tests/test_aiops_reasoning_trace.py \
  tests/test_evaluation_artifacts.py \
  tests/test_evaluation_runner.py \
  tests/test_snapshot_benchmark_cli.py -q
```

Expected: all selected tests pass with only project-preexisting documented skips.

- [ ] **Step 2: Run static checks and the ordinary offline suite**

```powershell
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/pyright.exe
./.venv/Scripts/python.exe -m pytest
```

Expected: Ruff passes, Pyright reports `0 errors, 0 warnings`, and the ordinary suite passes.

- [ ] **Step 3: Validate OpenSpec**

From repository root:

```powershell
openspec validate harden-aiops-decision-validation --strict
openspec validate --all
```

Expected: both commands pass.

- [ ] **Step 4: Update DomainBench documentation**

Document:

- `llm_confirmed` versus `deterministic_grounded_fallback`;
- the `evidence-driven-v3` contract and historical v2 compatibility rule;
- all deterministic fallback conditions;
- error categories and the rule that unavailable validators never produce evidence Replans;
- the manual-review recovery restriction;
- the exact offline commands and observed results.

Do not claim the real acceptance passed before Step 5 completes.

- [ ] **Step 5: Run exactly one real APY-013 acceptance**

From `apps/backend`, using the configured local user and knowledge base already used by the prior real run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
$EvalOwner='user_c88807ff36b74a038b9e1ea31a389cfc'
$EvalKnowledgeBase='kb_user_c88807ff36b74a038b9e1ea31a389cfc'
& ./.venv/Scripts/python.exe scripts/run_snapshot_benchmark.py `
  --scenario APY-013 `
  --suite-version evidence-v2 `
  --runs 1 `
  --adapter application `
  --config ../../config/project.json `
  --rag-mode on `
  --owner-user-id $EvalOwner `
  --knowledge-base-id $EvalKnowledgeBase `
  --output var/benchmarks/APY-013-resilient-validation-real.json
```

Expected acceptance:

- non-null scoreable root-cause decision;
- validation origin is `llm_confirmed` or `deterministic_grounded_fallback`;
- no `decision_validation_gap` Replanner;
- no `GetDatabaseMetrics` call;
- no `missing_root_cause_decision` failure;
- Benchmark `passed=true`.

If the command fails, do not rerun it. Query only the persisted task, decision, validation, Replanner, evidence, and tool audit records for that Run ID and report the new structured error category.

- [ ] **Step 6: Record final evidence and complete OpenSpec tasks**

Add the real Run ID, score, validation origin, duration, tool list, and non-secret failure category (if any) to `docs/aiops/agentpy-domainbench.md`. Mark only actually completed checklist items in the OpenSpec tasks file.

- [ ] **Step 7: Run final diff checks and commit**

```powershell
git diff --check
git status --short
git add docs/aiops/agentpy-domainbench.md openspec/changes/harden-aiops-decision-validation/tasks.md
git commit -m "docs: record resilient validator acceptance"
```

Expected: only intended source, tests, docs, and OpenSpec files are tracked; `var/`, credentials, caches, and local configuration remain untracked or ignored.
