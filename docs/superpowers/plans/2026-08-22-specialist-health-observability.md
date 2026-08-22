# Specialist Health and Analysis Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Represent Multi-Agent Specialist evidence collection and structured analysis health independently, persist the bounded audit trail end to end, and preserve existing central diagnosis and safety behavior.

**Architecture:** Extend the existing immutable `SpecialistResult` contract with two orthogonal health dimensions and derive the legacy terminal status from them. Keep the existing LangGraph execution, idempotency coordinator, evidence ownership checks, central Fact Adapter/Decision/Validator, artifact archive, and PostgreSQL recording paths; add only allowlisted projections and a deadline-aware correction guard to the existing bounded structured role helper.

**Tech Stack:** Python 3.12, Pydantic v2, LangGraph, PostgreSQL-backed repositories/checkpoints, pytest/pytest-asyncio, Ruff, Pyright.

## Global Constraints

- Do not change root-cause scoring, Ground Truth, score thresholds, central decision authority, or recovery authorization.
- Do not expose prompts, raw model responses, exception messages, raw CLS logs, credentials, Oracle fields, Ground Truth, or hidden reasoning.
- Do not add dependencies or enable production `auto` Multi-Agent routing.
- Preserve existing `terminalStatus` readers and immutable historical artifacts; absent new health fields mean `unknown`.
- Counts and error codes must use bounded allowlists, and replay must continue to use the existing `ExecutionCoordinator` identities.
- Follow-up questions are advisory: they cannot create Evidence, change dispositions, authorize recovery, or make a successfully validated analysis unhealthy.

## File Map

- `apps/backend/src/super_ai/aiops/specialists.py`: typed Specialist health contract, validation, legacy status derivation, and checksum.
- `apps/backend/src/super_ai/aiops/decision_validation.py`: optional pre-correction retry guard on the shared bounded structured role helper.
- `apps/backend/src/super_ai/aiops/investigation_runtime.py`: derive evidence/analysis health from tools, deadlines, budgets, and structured output; strengthen the Evidence Analysis JSON contract.
- `apps/backend/src/super_ai/aiops/evidence_aggregation.py`: deterministic per-role health maps and checkpoint projection.
- `apps/backend/src/super_ai/aiops/diagnostics.py`: LangGraph payload compatibility, Diagnostic Step/checkpoint/SSE projections, and existing central routing.
- `apps/backend/src/super_ai/evaluation/artifacts.py`: safe artifact allowlist and historical defaults.
- `apps/backend/src/super_ai/evaluation/recording.py`: reconstruct typed metrics from PostgreSQL terminal records.
- `apps/backend/src/super_ai/evaluation/history.py`: permit the new bounded metric keys in snapshot/live envelopes.
- `apps/backend/src/super_ai/evaluation/live/cli.py`: emit success and failure-path Live metrics.
- `apps/backend/tests/test_aiops_specialist_contracts.py`: unit contract/checksum/backward compatibility tests.
- `apps/backend/tests/test_aiops_specialist_model_roles.py`: executor semantics, retry guard, prompt contract, and deadline tests.
- `apps/backend/tests/test_aiops_multi_agent_runtime.py`: aggregation, LangGraph persistence, replay, and unchanged safety tests.
- `apps/backend/tests/test_aiops_evidence_packets.py`: update Specialist fixtures and preserve packet aggregation behavior.
- `apps/backend/tests/test_aiops_network_resume.py`: update Specialist fixtures and preserve replay/resume behavior.
- `apps/backend/tests/test_aiops_v4_workflow.py`: update Specialist fixtures and preserve central V4 workflow behavior.
- `apps/backend/tests/test_evaluation_artifacts.py`: safe projection and historical artifact tests.
- `apps/backend/tests/test_evaluation_recording.py`: PostgreSQL reconstruction tests.
- `apps/backend/tests/test_live_evaluation_scoring.py`: Live success/failure metric projection and unchanged score tests.

---

### Task 1: Define the Specialist health contract and legacy status derivation

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/specialists.py`
- Modify: `apps/backend/tests/test_aiops_specialist_contracts.py`
- Modify: `apps/backend/tests/test_aiops_evidence_packets.py`
- Modify: `apps/backend/tests/test_aiops_network_resume.py`
- Modify: `apps/backend/tests/test_aiops_v4_workflow.py`

**Interfaces:**
- Produces: `SpecialistEvidenceStatus`, `SpecialistAnalysisStatus`, `SpecialistAnalysisErrorCode`, `derive_specialist_terminal_status(evidence_status, analysis_status) -> SpecialistTerminalStatus`, and `specialist_result_legacy_checksum(result) -> str`.
- Produces: new `SpecialistResult.create(...)` keyword parameters `evidence_status`, `analysis_status`, `analysis_error_code`, `analysis_attempt_count`, `soft_deadline_exceeded`, `hard_deadline_exceeded`, and `expected_tool_count`.
- Preserves: `terminal_status`, `completed_steps`, `model_call_count`, `duration_ms`, and deterministic `result_checksum`.

- [ ] **Step 1: Write failing contract tests**

Add table-driven tests that construct results with the new fields and assert the exact compatibility matrix, including follow-up questions having no effect on the derived terminal status:

```python
@pytest.mark.parametrize(
    ("evidence_status", "analysis_status", "expected"),
    [
        ("complete", "complete", "completed"),
        ("complete", "degraded", "inconclusive"),
        ("complete", "timeout", "inconclusive"),
        ("partial", "complete", "inconclusive"),
        ("none", "timeout", "timeout"),
        ("none", "failed", "failed"),
        ("none", "skipped", "failed"),
    ],
)
def test_specialist_health_derives_legacy_terminal_status(
    evidence_status: str, analysis_status: str, expected: str
) -> None:
    result = _result(
        terminal_status=expected,
        evidence_status=evidence_status,
        analysis_status=analysis_status,
    )
    assert result.terminal_status == expected


def test_follow_up_questions_do_not_degrade_a_complete_analysis() -> None:
    result = _result(
        terminal_status="completed",
        evidence_status="complete",
        analysis_status="complete",
        unresolved_questions=("Which deploy first changed checkout latency?",),
    )
    assert result.terminal_status == "completed"
    assert result.follow_up_question_count == 1
```

Also assert invalid enum/error values, negative or over-limit counts, inconsistent `terminal_status`, and checksum changes when any health field changes. The core contract remains strict; old checkpoint compatibility is handled only by the deserialization adapter in Task 3, while historical evaluation artifacts remain unknown as required in Task 4.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run from `apps/backend`:

```powershell
uv run pytest tests/test_aiops_specialist_contracts.py -q
```

Expected: FAIL because the new type aliases, fields, and derivation function do not exist.

- [ ] **Step 3: Implement the immutable bounded contract**

Add the exact bounded types and derivation function:

```python
SpecialistEvidenceStatus = Literal["complete", "partial", "none"]
SpecialistAnalysisStatus = Literal[
    "complete", "degraded", "timeout", "failed", "skipped"
]
SpecialistAnalysisErrorCode = Literal[
    "parse_error",
    "schema_validation_failed",
    "scope_rejected",
    "provider_4xx",
    "provider_5xx",
    "provider_timeout",
    "retry_exhausted",
    "retry_skipped_insufficient_deadline",
    "specialist_soft_deadline_expired",
    "specialist_hard_deadline_expired",
    "specialist_model_budget_exhausted",
]


def derive_specialist_terminal_status(
    evidence_status: SpecialistEvidenceStatus,
    analysis_status: SpecialistAnalysisStatus,
) -> SpecialistTerminalStatus:
    if evidence_status == "complete" and analysis_status == "complete":
        return "completed"
    if evidence_status == "none":
        return "timeout" if analysis_status == "timeout" else "failed"
    return "inconclusive"
```

Add fields to `SpecialistResult`, validate `0 <= analysis_attempt_count <= 2`, `0 <= expected_tool_count <= 3`, `len(completed_steps) <= expected_tool_count`, and require `analysis_error_code is None` for `analysis_status == "complete"`. Derive `completed_tool_count` and `follow_up_question_count` as properties. Reject a caller-provided `terminal_status` that differs from the derivation. Include every new persisted field in `_calculate_result_checksum()`. Preserve the former checksum material verbatim in `specialist_result_legacy_checksum(result)` so Task 3 can authenticate old payloads before migration.

Update every existing `SpecialistResult.create()` call in `test_aiops_evidence_packets.py`, `test_aiops_network_resume.py`, and `test_aiops_v4_workflow.py` with internally consistent explicit health fields. Do not add permissive production defaults merely to keep fixtures compiling.

- [ ] **Step 4: Run contract tests and static checks**

```powershell
uv run pytest tests/test_aiops_specialist_contracts.py tests/test_aiops_evidence_packets.py tests/test_aiops_network_resume.py tests/test_aiops_v4_workflow.py -q
uv run ruff check src/super_ai/aiops/specialists.py tests/test_aiops_specialist_contracts.py tests/test_aiops_evidence_packets.py tests/test_aiops_network_resume.py tests/test_aiops_v4_workflow.py
uv run pyright src/super_ai/aiops/specialists.py
```

Expected: all commands exit 0; contract tests pass and checksum mismatch remains detectable.

- [ ] **Step 5: Commit the contract slice**

```powershell
git add apps/backend/src/super_ai/aiops/specialists.py apps/backend/tests/test_aiops_specialist_contracts.py apps/backend/tests/test_aiops_evidence_packets.py apps/backend/tests/test_aiops_network_resume.py apps/backend/tests/test_aiops_v4_workflow.py
git commit -m "feat(aiops): separate specialist evidence and analysis health"
```

---

### Task 2: Make Evidence Analysis structured retries deadline-aware

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Modify: `apps/backend/src/super_ai/aiops/investigation_runtime.py`
- Modify: `apps/backend/tests/test_aiops_specialist_model_roles.py`

**Interfaces:**
- Consumes: Task 1 health types and derivation.
- Produces: optional `retry_guard: Callable[[], bool] | None` and `attempt_timeout_seconds: float | None` on `invoke_bounded_structured_role`; defaults preserve existing callers, while a false guard returns `error_code="retry_skipped_insufficient_deadline"` without a second provider call.
- Produces: private typed `SpecialistRoleInvocation[_RoleOutput]` from `_run_role()` carrying `value`, `error_category`, `error_code`, `attempt_count`, `error_phase`, `retryable`, and `http_status_class` without raw provider data.
- Produces: `SpecialistExecutor._result(...)` parameters for both health dimensions and safe audit fields.

- [ ] **Step 1: Write failing retry and executor semantic tests**

Add tests using the existing fake chat model, deterministic clock, fake tool runtime, and `ExecutionCoordinator` fixtures:

```python
@pytest.mark.asyncio
async def test_bounded_role_skips_correction_when_retry_guard_is_false() -> None:
    model = _SequenceModel([{"bad": "shape"}, _valid_analysis_payload()])
    outcome = await invoke_bounded_structured_role(
        model=model,
        schema=SpecialistEvidenceAnalysisOutput,
        prompt="analyze",
        correction_prompt="correct",
        role="evidence_analysis",
        retry_guard=lambda: False,
        attempt_timeout_seconds=30.0,
    )
    assert outcome.value is None
    assert outcome.attempts == 1
    assert outcome.error_code == "retry_skipped_insufficient_deadline"
    assert model.call_count == 1


@pytest.mark.asyncio
async def test_follow_up_questions_keep_log_analysis_complete() -> None:
    result = await _execute_log_specialist(
        analysis=_valid_analysis_payload(
            unresolved_questions=["Check whether another service shares the pool."]
        )
    )
    assert result.evidence_status == "complete"
    assert result.analysis_status == "complete"
    assert result.terminal_status == "completed"


@pytest.mark.asyncio
async def test_retry_exhaustion_preserves_complete_runtime_evidence() -> None:
    result = await _execute_runtime_specialist(analysis_responses=[{}, {}])
    assert result.evidence_status == "complete"
    assert result.analysis_status == "degraded"
    assert result.analysis_error_code == "retry_exhausted"
    assert result.analysis_attempt_count == 2
    assert result.terminal_status == "inconclusive"
    assert len(result.evidence_ids) == result.expected_tool_count == 3
```

Add cases for analysis hard deadline (`complete/timeout/inconclusive`), partial tool execution (`partial` regardless of analysis prose), no Evidence with timeout/failure, skipped analysis for model budget, and a replay asserting the model/tool call counters are not incremented.

- [ ] **Step 2: Run the model-role tests and confirm RED**

```powershell
uv run pytest tests/test_aiops_specialist_model_roles.py -q
```

Expected: FAIL on the absent `retry_guard` and absent health fields; existing structured-role tests should still execute.

- [ ] **Step 3: Add the shared retry guard without changing default behavior**

Add an optional per-attempt timeout and change only the invocation/correction branches in `invoke_bounded_structured_role`:

```python
StructuredRoleRetryGuard = Callable[[], bool]

async def invoke_bounded_structured_role(
    *,
    # existing parameters unchanged
    retry_guard: StructuredRoleRetryGuard | None = None,
    attempt_timeout_seconds: float | None = None,
) -> BoundedStructuredRoleOutcome[_StructuredRoleModel]:
    response = (
        await invoker.ainvoke(current_prompt)
        if attempt_timeout_seconds is None
        else await asyncio.wait_for(
            invoker.ainvoke(current_prompt), timeout=attempt_timeout_seconds
        )
    )
    # ... after an invalid response and before the correction call
    if attempt < maximum_attempts:
        if retry_guard is not None and not retry_guard():
            return BoundedStructuredRoleOutcome(
                value=None,
                error_category="retry_exhausted",
                attempts=attempt,
                audits=tuple(audits),
                error_code="retry_skipped_insufficient_deadline",
                error_phase="structured_parse",
                retryable=False,
            )
        current_prompt = f"{prompt}\n\n{correction_prompt}"
        continue
```

Classify the new `TimeoutError` through the existing safe model-failure path as `error_code="timeout"`; persist no exception text. Both defaults are `None`, preserving Validator, Planner, and all existing callers exactly.

- [ ] **Step 4: Implement executor health classification and the explicit JSON contract**

Have the executor compute evidence status from the accepted plan and persisted outputs:

```python
def _evidence_status(*, expected: int, completed: int) -> SpecialistEvidenceStatus:
    if completed == 0:
        return "none"
    return "complete" if completed == expected else "partial"
```

Normalize shared invoker metadata through one private `_specialist_analysis_failure(invocation) -> tuple[SpecialistAnalysisStatus, SpecialistAnalysisErrorCode]`: structured parse exhaustion -> `degraded/retry_exhausted`; a skipped correction after one invalid attempt -> `degraded/retry_skipped_insufficient_deadline`; `error_code="timeout"` -> `timeout/provider_timeout`; HTTP class `4xx` or `5xx` -> `failed/provider_4xx` or `failed/provider_5xx`; scope validation -> `failed/scope_rejected`; insufficient budget/deadline before any analysis call -> `skipped/specialist_model_budget_exhausted` or the corresponding deadline code. Do not persist lower-level auth/rate-limit/connection strings outside this normalization.

Add `evidence_analysis_attempt_timeout_seconds: float = 30.0` and `retry_scheduling_margin_seconds: float = 1.0` keyword-only constructor parameters to `SpecialistExecutor`; validate both as positive and pass the attempt timeout to the shared helper. The retry guard is exactly `self._remaining_hard_seconds(context, assignment) >= self._evidence_analysis_attempt_timeout_seconds + self._retry_scheduling_margin_seconds`.

Change `_run_role()` to return `SpecialistRoleInvocation` rather than `(value, errorCategory)`. Serialize and restore `attempts`, `errorCategory`, `errorCode`, `errorPhase`, `retryable`, and `httpStatusClass` in the existing idempotency coordinator output, validating them against the shared safe types. This makes attempt counts and normalized errors reliable after replay.

Append to the Evidence Analysis prompt an explicit public contract and synthetic example:

```text
Return exactly tested_hypotheses, fact_candidates, proposed_assessments, and
unresolved_questions as one JSON object; no wrappers or extra fields. Every
Evidence ID must be in ownedEvidenceIds and every hypothesis must be assigned.
unresolved_questions may be non-empty and are advisory only.
Example: {"tested_hypotheses":["hypothesis-a"],"fact_candidates":[],
"proposed_assessments":[],"unresolved_questions":[]}
```

Do not persist the prompt or response. Return tool-derived claims unchanged when analysis is degraded, timed out, failed, or skipped.

- [ ] **Step 5: Run focused executor and shared-helper regression tests**

```powershell
uv run pytest tests/test_aiops_specialist_model_roles.py -q
uv run pytest tests/test_aiops_decision_validation.py -q
uv run ruff check src/super_ai/aiops/decision_validation.py src/super_ai/aiops/investigation_runtime.py tests/test_aiops_specialist_model_roles.py
uv run pyright src/super_ai/aiops/decision_validation.py src/super_ai/aiops/investigation_runtime.py
```

Expected: all commands exit 0; default two-attempt behavior remains unchanged and the guarded path makes exactly one call.

- [ ] **Step 6: Commit the executor slice**

```powershell
git add apps/backend/src/super_ai/aiops/decision_validation.py apps/backend/src/super_ai/aiops/investigation_runtime.py apps/backend/tests/test_aiops_specialist_model_roles.py
git commit -m "fix(aiops): stabilize specialist evidence analysis health"
```

---

### Task 3: Project health through aggregation, LangGraph checkpoints, and events

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/evidence_aggregation.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_multi_agent_runtime.py`

**Interfaces:**
- Consumes: Task 1 `SpecialistResult` fields.
- Produces: `AggregatedInvestigation.specialist_evidence_statuses`, `specialist_analysis_statuses`, `specialist_analysis_error_codes`, `specialist_analysis_attempt_counts`, `specialist_follow_up_question_counts`, and deadline/tool-count maps.
- Preserves: existing `specialist_statuses`, `terminal_failure_category`, central Fact Adapter inputs, routing, Decision, Validator, and Recovery Policy.

- [ ] **Step 1: Write failing aggregation and persistence tests**

Construct one complete/degraded Runtime result and one complete/complete Log result, aggregate them, and assert exact bounded projections:

```python
assert aggregated.specialist_evidence_statuses == {
    "log": "complete", "runtime": "complete"
}
assert aggregated.specialist_analysis_statuses == {
    "log": "complete", "runtime": "degraded"
}
assert aggregated.specialist_analysis_error_codes == {
    "runtime": "retry_exhausted"
}
assert aggregated.specialist_follow_up_question_counts == {"log": 1, "runtime": 0}
assert aggregated.specialist_statuses == {
    "log": "completed", "runtime": "inconclusive"
}
```

Add an async LangGraph test asserting the `evidence_aggregator` Diagnostic Step, checkpoint, and `aiops.specialist_aggregation` event include only these safe maps and per-role counts. Add a historical checkpoint payload test where `_specialist_result_from_payload()` receives only old fields, first verifies `resultChecksum` with `specialist_result_legacy_checksum()`, then returns a migrated result with a newly calculated checksum. The compatibility mapping is: Evidence exists and completed steps equal expected count -> `complete`; Evidence exists but fewer steps completed -> `partial`; no Evidence -> `none`; `completed -> analysis complete`, evidence-bearing `inconclusive -> analysis degraded`, timeouts -> analysis timeout, other failures -> analysis failed. Preserve evidence-bearing failed/timeout branches. Add a tampered legacy-checksum rejection test and a replay test proving the same migrated Specialist checksum and audit event are reused once.

- [ ] **Step 2: Run Multi-Agent runtime tests and confirm RED**

```powershell
uv run pytest tests/test_aiops_multi_agent_runtime.py -q
```

Expected: FAIL because the aggregate maps and serialized fields are absent.

- [ ] **Step 3: Extend deterministic aggregation and serialization**

Add immutable sorted maps to `AggregatedInvestigation`, populate them from results, and include them in `to_checkpoint_payload()`. Keep `terminal_failure_category` based on the legacy terminal status so central routing behavior does not change.

Extend `_specialist_result_payload()`, `_failed_specialist_result()`, and `_specialist_event()` with the exact camelCase keys below. Change the reader to `_specialist_result_from_payload(payload, *, legacy_expected_tool_count: int | None = None) -> SpecialistResult`; reorder `_aggregate_specialist_results()` so assignments are reconstructed before results, and pass `assignment.maximum_tool_steps` only when a legacy payload lacks `expectedToolCount`.

```python
{
    "evidenceStatus": result.evidence_status,
    "analysisStatus": result.analysis_status,
    "analysisErrorCode": result.analysis_error_code,
    "analysisAttemptCount": result.analysis_attempt_count,
    "followUpQuestionCount": result.follow_up_question_count,
    "softDeadlineExceeded": result.soft_deadline_exceeded,
    "hardDeadlineExceeded": result.hard_deadline_exceeded,
    "completedToolCount": result.completed_tool_count,
    "expectedToolCount": result.expected_tool_count,
}
```

For old checkpoint payloads only, validate the old checksum before constructing the migrated result. Set `expectedToolCount=len(completedSteps)` for old `completed` results; otherwise use `max(len(completedSteps), legacy_expected_tool_count or 0)` from the reconstructed assignment. Derive evidence health by comparing completed/expected counts only after confirming Evidence IDs are present. The migrated in-memory result receives the new checksum and is used only for resumed execution. Do not rewrite old archive artifacts, and do not include raw follow-up text in aggregation summary or SSE.

- [ ] **Step 4: Preserve central decision and safety with regression assertions**

In the existing trusted compound-pattern test, assert that the same evidence IDs reach Fact Adapter and the same deterministic root-cause decision, `manual_review`/recovery authorization, verification, and cleanup values are produced before and after the health projection. Do not update expected scores or thresholds.

- [ ] **Step 5: Run aggregation regression and static checks**

```powershell
uv run pytest tests/test_aiops_multi_agent_runtime.py -q
uv run pytest tests/test_aiops_specialist_contracts.py tests/test_aiops_specialist_model_roles.py -q
uv run ruff check src/super_ai/aiops/evidence_aggregation.py src/super_ai/aiops/diagnostics.py tests/test_aiops_multi_agent_runtime.py
uv run pyright src/super_ai/aiops/evidence_aggregation.py src/super_ai/aiops/diagnostics.py
```

Expected: all commands exit 0; central diagnosis/safety assertions are unchanged.

- [ ] **Step 6: Commit the graph projection slice**

```powershell
git add apps/backend/src/super_ai/aiops/evidence_aggregation.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_multi_agent_runtime.py
git commit -m "feat(aiops): persist specialist health through aggregation"
```

---

### Task 4: Persist safe Specialist health in artifacts, PostgreSQL, and Live metrics

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/src/super_ai/evaluation/recording.py`
- Modify: `apps/backend/src/super_ai/evaluation/history.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/tests/test_evaluation_artifacts.py`
- Modify: `apps/backend/tests/test_evaluation_recording.py`
- Modify: `apps/backend/tests/test_live_evaluation_scoring.py`

**Interfaces:**
- Consumes: Task 3 `evidence_aggregator` Diagnostic Step fields.
- Produces: extended `SpecialistRoleAudit` and `InvestigationBenchmarkMetrics` plus the exact persisted metric keys listed in the confirmed design.
- Preserves: strict per-evaluation-kind allowlists and readability of old envelopes.

- [ ] **Step 1: Write failing artifact safety and compatibility tests**

Add a safe projection test with one Log follow-up and one Runtime retry exhaustion, then assert:

```python
runtime = next(role for role in artifact.investigation.roles if role.role == "runtime")
assert runtime.evidence_status == "complete"
assert runtime.analysis_status == "degraded"
assert runtime.analysis_error_code == "retry_exhausted"
assert runtime.analysis_attempt_count == 2
assert runtime.completed_tool_count == runtime.expected_tool_count == 3
assert runtime.follow_up_question_count == 0
```

Inject an unknown role, unknown status/error code, negative counts, counts above 16, raw questions, `prompt`, `rawResponse`, `exception`, credentials, and Oracle keys; assert these are rejected/dropped using the existing artifact safety behavior and never appear in `repr(artifact)` or terminal JSON. Add a historical test proving an old artifact is readable with every new field—including attempt/question/tool counts and deadline flags—set to `None`/unknown rather than zero/false/success. Assert historical roles are excluded from every new basis-point denominator.

- [ ] **Step 2: Write failing PostgreSQL and Live metric tests**

Assert `investigation_metrics_from_persisted_result()` reconstructs these exact fields from terminal metrics:

```python
assert metrics.role_evidence_statuses == (("log", "complete"), ("runtime", "complete"))
assert metrics.role_analysis_statuses == (("log", "complete"), ("runtime", "degraded"))
assert metrics.specialist_evidence_completion_basis_points == 10_000
assert metrics.specialist_analysis_completion_basis_points == 5_000
assert metrics.specialist_degradation_basis_points == 5_000
```

Add Live success- and failure-path assertions for all new maps and rates, including deadline and structured-retry basis points. Assert the existing `total`, `rootCauseTop1Correct`, `evidenceRecallBasisPoints`, `verificationPassed`, and `cleanupSucceeded` values do not change.

- [ ] **Step 3: Run evaluation tests and confirm RED**

```powershell
uv run pytest tests/test_evaluation_artifacts.py tests/test_evaluation_recording.py tests/test_live_evaluation_scoring.py -q
```

Expected: FAIL because the new audit fields, metric allowlist keys, and projections do not exist.

- [ ] **Step 4: Implement bounded artifact extraction and typed metrics**

Extend `SpecialistRoleAudit` with optional historical-safe fields and allowlist only:

```python
evidence_status: str | None = None
analysis_status: str | None = None
analysis_error_code: str | None = None
analysis_attempt_count: int | None = None
follow_up_question_count: int | None = None
soft_deadline_exceeded: bool | None = None
hard_deadline_exceeded: bool | None = None
completed_tool_count: int | None = None
expected_tool_count: int | None = None
```

Parse only known roles, `complete|partial|none`, `complete|degraded|timeout|failed|skipped`, the design's safe error codes, booleans, attempts `0..2`, and tool/question counts `0..16`. Never copy the role dictionary wholesale.

Extend `InvestigationBenchmarkMetrics`, `_investigation_process_metrics()`, the successful Live path, `investigation_metrics_from_persisted_result()`, and `_METRIC_KEYS` with:

```text
specialistEvidenceStatuses
specialistAnalysisStatuses
specialistAnalysisErrorCodes
specialistAnalysisAttemptCounts
specialistFollowUpQuestionCounts
specialistEvidenceCompletionBasisPoints
specialistAnalysisCompletionBasisPoints
specialistDegradationBasisPoints
specialistDeadlineHitBasisPoints
specialistStructuredRetryBasisPoints
```

Compute basis points deterministically over roles whose complete new health metadata is present only; if no role has the new fields, omit the new metric maps/rates from the envelope instead of emitting zero as a claim of failure. `_required_*` PostgreSQL readers become optional readers for these new keys only; old required metrics stay required.

- [ ] **Step 5: Run evaluation tests and static checks**

```powershell
uv run pytest tests/test_evaluation_artifacts.py tests/test_evaluation_recording.py tests/test_live_evaluation_scoring.py tests/test_aiops_evidence_packets.py tests/test_aiops_network_resume.py tests/test_aiops_v4_workflow.py -q
uv run ruff check src/super_ai/evaluation/artifacts.py src/super_ai/evaluation/recording.py src/super_ai/evaluation/history.py src/super_ai/evaluation/live/cli.py tests/test_evaluation_artifacts.py tests/test_evaluation_recording.py tests/test_live_evaluation_scoring.py
uv run pyright src/super_ai/evaluation/artifacts.py src/super_ai/evaluation/recording.py src/super_ai/evaluation/history.py src/super_ai/evaluation/live/cli.py
```

Expected: all commands exit 0; new metrics survive archive/PostgreSQL round-trips without private payloads, and historical tests pass.

- [ ] **Step 6: Commit the persistence slice**

```powershell
git add apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/src/super_ai/evaluation/recording.py apps/backend/src/super_ai/evaluation/history.py apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/tests/test_evaluation_artifacts.py apps/backend/tests/test_evaluation_recording.py apps/backend/tests/test_live_evaluation_scoring.py
git commit -m "feat(eval): record specialist evidence and analysis health"
```

---

### Task 5: Focused regression and one real forced-Multi canary

**Files:**
- Modify only if a focused regression exposes a defect in the files listed in Tasks 1-4.
- Record: existing Evaluation Archive and PostgreSQL terminal result through the normal Live command; do not hand-edit artifacts.

**Interfaces:**
- Consumes: all prior tasks and the existing Order Pool Live scenario, active 30-card RAG, CLS configuration, configured primary model, configured Specialist model, scorer, and archive recorder.
- Produces: one persisted forced-Multi acceptance run; it does not enable production `auto` routing.

- [ ] **Step 1: Run the focused offline suite**

From `apps/backend`:

```powershell
uv run pytest tests/test_aiops_specialist_contracts.py tests/test_aiops_specialist_model_roles.py tests/test_aiops_multi_agent_runtime.py tests/test_aiops_evidence_packets.py tests/test_aiops_network_resume.py tests/test_aiops_v4_workflow.py tests/test_evaluation_artifacts.py tests/test_evaluation_recording.py tests/test_live_evaluation_scoring.py -q
```

Expected: all focused tests pass. Do not run the full pytest suite unless a cross-module failure indicates a high-risk regression.

- [ ] **Step 2: Run focused lint and type checking**

```powershell
uv run ruff check src/super_ai/aiops/specialists.py src/super_ai/aiops/decision_validation.py src/super_ai/aiops/investigation_runtime.py src/super_ai/aiops/evidence_aggregation.py src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/artifacts.py src/super_ai/evaluation/recording.py src/super_ai/evaluation/history.py src/super_ai/evaluation/live/cli.py tests/test_aiops_specialist_contracts.py tests/test_aiops_specialist_model_roles.py tests/test_aiops_multi_agent_runtime.py tests/test_aiops_evidence_packets.py tests/test_aiops_network_resume.py tests/test_aiops_v4_workflow.py tests/test_evaluation_artifacts.py tests/test_evaluation_recording.py tests/test_live_evaluation_scoring.py
uv run pyright src/super_ai/aiops/specialists.py src/super_ai/aiops/decision_validation.py src/super_ai/aiops/investigation_runtime.py src/super_ai/aiops/evidence_aggregation.py src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/artifacts.py src/super_ai/evaluation/recording.py src/super_ai/evaluation/history.py src/super_ai/evaluation/live/cli.py
```

Expected: Ruff and Pyright exit 0.

- [ ] **Step 3: Execute one real Order Pool forced-Multi canary**

First discover the canonical scenario ID and supported CLI syntax without guessing:

```powershell
uv run python -m super_ai.evaluation.live.cli --help
rg -n "Order Pool|order.pool|APY-" src/super_ai/evaluation tests -g "*.py" -g "*.yaml"
```

Then run the repository's documented Live command with `--investigation-strategy multi` and the discovered Order Pool scenario ID, preserving the configured 30-card RAG, CLS, model settings, and normal recorder. Expected command outcome: a terminal persisted run, not an unrecorded debug invocation.

- [ ] **Step 4: Verify the canary in both persistence surfaces**

Use the existing evaluation history/archive inspection command and repository query path to assert:

```text
Runtime evidenceStatus = complete
Log evidenceStatus = complete
Log analysisStatus = complete (even with follow-up questions)
Runtime analysisStatus and safe analysisErrorCode are present
Root Cause Top-1 = correct
Evidence Recall = accepted baseline
security hard gate = pass
verification = pass
cleanup = pass
effective strategy = multi
production auto routing = unchanged/disabled
```

Also verify no prompt, raw response, exception body, raw CLS log, credential, Ground Truth, Oracle field, or hidden reasoning is present in the archive or PostgreSQL metric JSON.

- [ ] **Step 5: Commit any canary-driven localized correction and final verification note**

If the canary required no code change, do not create an empty commit. If a localized correction was required, rerun Steps 1-2 and commit only that correction:

```powershell
git add apps/backend/src/super_ai/aiops/specialists.py apps/backend/src/super_ai/aiops/decision_validation.py apps/backend/src/super_ai/aiops/investigation_runtime.py apps/backend/src/super_ai/aiops/evidence_aggregation.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/src/super_ai/evaluation/recording.py apps/backend/src/super_ai/evaluation/history.py apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/tests/test_aiops_specialist_contracts.py apps/backend/tests/test_aiops_specialist_model_roles.py apps/backend/tests/test_aiops_multi_agent_runtime.py apps/backend/tests/test_aiops_evidence_packets.py apps/backend/tests/test_aiops_network_resume.py apps/backend/tests/test_aiops_v4_workflow.py apps/backend/tests/test_evaluation_artifacts.py apps/backend/tests/test_evaluation_recording.py apps/backend/tests/test_live_evaluation_scoring.py
git commit -m "fix(aiops): harden specialist health canary projection"
```

Finally run:

```powershell
git status --short --branch
git log -5 --oneline
```

Expected: feature branch contains the design, plan, and implementation commits; no unrelated user changes are staged; the worktree is clean after committed corrections.
