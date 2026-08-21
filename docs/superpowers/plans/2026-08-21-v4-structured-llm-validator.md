# V4 Structured LLM Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the conditionally routed V4 LLM Validator use the configured structured-output method, retain V4 budget/deadline audits, and persist precise secret-safe failure metadata.

**Architecture:** Extend the existing structured root-cause validation helper with an optional invocation callback, then supply a V4-owned budget-aware callback from `_llm_validator_v4()`. The deterministic router and recovery policy remain unchanged; semantic failures preserve the grounded diagnosis but force manual review.

**Tech Stack:** Python 3.10+, LangChain `ChatModel.with_structured_output`, Pydantic, LangGraph, pytest/pytest-asyncio, Ruff, Pyright.

## Global Constraints

- Preserve `requires_llm_validation()` and all existing allowlisted route reasons.
- Use the dedicated Validator model and its configured structured-output method.
- Permit at most one format-only retry.
- Count every attempted model invocation against the V4 budget and audit it.
- Classify budget exhaustion and hard-deadline exhaustion consistently in both
  Validator metadata and model-call audits.
- Never persist raw model output, exception text, prompt text, secrets, private reasoning, or ground truth.
- Any semantic failure forces `manual_review`; it never grants execution authority.
- Do not add a dependency, external service, native binary, or license obligation.
- Do not run the full pytest suite; use only the focused commands listed below.

---

## File Structure

- Modify `apps/backend/src/super_ai/aiops/decision_validation.py`: expose a small typed invocation callback and route each structured Validator attempt through it when supplied.
- Modify `apps/backend/src/super_ai/aiops/diagnostics.py`: add the budget/deadline/audit adapter and replace the V4 free-form Validator path with the shared structured helper.
- Modify `apps/backend/tests/test_aiops_decision_validation.py`: prove callback use does not change parsing/retry semantics.
- Modify `apps/backend/tests/test_aiops_v4_workflow.py`: prove provider method propagation, retry accounting, safe classifications, fail-closed recovery, and optional routing.
- Read-only verification target `apps/backend/tests/test_aiops_validator_routing.py`: retain the existing optional-gate contract.
- Append `docs/aiops/agentpy-domainbench.md` only after a real canary is persisted and cleanup is independently verified.

### Task 1: Add a reusable structured invocation seam

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py:5-10,179-181,344-369`
- Test: `apps/backend/tests/test_aiops_decision_validation.py`

**Interfaces:**
- Consumes: an already configured `_AsyncInvoker` returned by `_structured_invoker()` and the current prompt.
- Produces: `StructuredValidationInvoke = Callable[[_AsyncInvoker, object], Awaitable[object]]`, optional `invoke` argument on `invoke_structured_root_cause_validation()`, and a safe callback exception carrying allowlisted runtime-failure metadata.

- [ ] **Step 1: Write the failing callback-and-retry test**

Add imports for `Awaitable` and `Callable` in production only when implementing. First add a test using an existing structured fake model plus this callback shape:

```python
calls: list[object] = []

async def invoke(invoker: object, prompt: object) -> object:
    calls.append(prompt)
    typed = cast(Any, invoker)
    return await typed.ainvoke(prompt)

outcome = await invoke_structured_root_cause_validation(
    model=model,
    prompt="Validate public evidence.",
    available_evidence_ids={"ev-1"},
    structured_output_method="json_mode",
    invoke=invoke,
)

assert outcome.decision is not None
assert outcome.attempts == 2
assert len(calls) == 2
assert model.structured_output_methods == ["json_mode"]
assert calls[1] != calls[0]
```

Configure the fake's first response as a structured envelope with a parsing
error and its second response as a valid `_RootCauseValidationSchema` envelope.

Also add a classification test which raises the new safe callback exception
with `model_call_budget_exhausted` and asserts the outcome retains that exact
code, `model_invoke` phase, and `retryable=False` without reading exception
text.

- [ ] **Step 2: Run the test and verify RED**

Run from `apps/backend`:

```powershell
uv run pytest tests/test_aiops_decision_validation.py -k "callback and retry" -o addopts='' -q -p no:cacheprovider
```

Expected: FAIL because `invoke_structured_root_cause_validation()` does not yet
accept `invoke`.

- [ ] **Step 3: Add the minimal typed invocation seam**

Implement this shape without changing default callers:

```python
from collections.abc import Awaitable, Callable, Mapping, Sequence, Set

StructuredValidationInvoke = Callable[[_AsyncInvoker, object], Awaitable[object]]


class SafeModelInvocationFailure(RuntimeError):
    """Carry only allowlisted runtime failure metadata across an invocation seam."""

    def __init__(self, failure: SafeModelFailure) -> None:
        super().__init__()
        self.failure = failure


async def _invoke_direct(invoker: _AsyncInvoker, input: object) -> object:
    return await invoker.ainvoke(input)


async def invoke_structured_root_cause_validation(
    *,
    model: ChatModel,
    prompt: str,
    available_evidence_ids: Set[str],
    structured_output_method: StructuredOutputMethod = "function_calling",
    invoke: StructuredValidationInvoke | None = None,
) -> StructuredValidationOutcome:
    # Existing structured setup remains unchanged.
    invoke_attempt = invoke or _invoke_direct
    # Existing two-attempt loop remains unchanged except:
    response = await invoke_attempt(invoker, current_prompt)
```

Extend `ValidationErrorCode` with `model_call_budget_exhausted` and
`hard_deadline_exceeded`. Make `classify_model_failure()` return
`exc.failure` for `SafeModelInvocationFailure`. Do not move schema validation
or ordinary provider-exception classification into the callback.

- [ ] **Step 4: Run helper tests and verify GREEN**

```powershell
uv run pytest tests/test_aiops_decision_validation.py -o addopts='' -q -p no:cacheprovider
```

Expected: all tests in the file PASS, including the new two-attempt callback
test and all unchanged direct-invocation tests.

- [ ] **Step 5: Commit the helper seam**

```powershell
git add apps/backend/src/super_ai/aiops/decision_validation.py apps/backend/tests/test_aiops_decision_validation.py
git commit -m "refactor: make validator invocation adaptable"
```

### Task 2: Route V4 semantic validation through structured output

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py:47-54,2085-2164,5129-5225`
- Test: `apps/backend/tests/test_aiops_v4_workflow.py:3854-4001`

**Interfaces:**
- Consumes: `invoke_structured_root_cause_validation()`, `_validator_chat_model()`, `_validator_model_name()`, `_validator_structured_output_method()`, `_ModelRuntime`, and a structured invoker exposing `ainvoke()`.
- Produces: `_invoke_v4_structured_model(runtime, role, invoker, prompt) -> object` and enriched `decision_validation` payload fields.

- [ ] **Step 1: Write the RED success-path test**

Add `test_v4_semantic_validator_uses_configured_json_mode` with a fake dedicated
Validator model:

```python
class ValidatorModel:
    def __init__(self) -> None:
        self.schema: type[object] | None = None
        self.structured_output_methods: list[object] = []

    def with_structured_output(
        self, schema: type[object], **kwargs: object
    ) -> ValidatorModel:
        self.schema = schema
        self.structured_output_methods.append(kwargs.get("method"))
        return self

    async def ainvoke(self, _prompt: object) -> object:
        assert self.schema is not None
        parsed = cast(Any, self.schema).model_validate(
            {
                "status": "valid",
                "evidenceIds": ["ev-1", "ev-2", "ev-3"],
                "unsupportedFields": [],
                "missingEvidence": [],
                "summary": "Every public candidate field is supported.",
            }
        )
        return {"parsed": parsed, "parsing_error": None}

class Provider:
    validator_structured_output_method = "json_mode"
    validator_model_name = "validator-test-model"

    def create_validator_model(self) -> ValidatorModel:
        return model

    def create_chat_model(self) -> ValidatorModel:
        return model
```

Invoke `_llm_validator_v4()` with the existing valid candidate state and assert:

```python
assert model.structured_output_methods == ["json_mode"]
assert validation["validationOrigin"] == "llm_semantic"
assert validation["semanticValidationStatus"] == "valid"
assert validation["semanticValidationAttempts"] == 1
assert validation["validationModel"] == "validator-test-model"
assert update["model_call_count"] == initial_model_count + 1
assert len(update["model_call_audits"]) == 1
assert "recovery_plan" not in update
```

- [ ] **Step 2: Write RED retry and safe-failure tests**

Add two focused cases:

1. First envelope has `parsing_error=ValueError()` and second is valid. Assert
   `semanticValidationAttempts == 2`, model count increases by two, two audits
   exist, and the second prompt contains the helper's format correction.
2. Both envelopes have parsing errors. Assert:

```python
assert validation["validationOrigin"] == "llm_failed"
assert validation["semanticValidationStatus"] == "failed"
assert validation["validationErrorCategory"] == "retry_exhausted"
assert validation["validationErrorCodes"] == ["structured_envelope_mismatch"]
assert validation["validationErrorPhase"] == "structured_parse"
assert validation["validationRetryable"] is False
assert recovery["mode"] == "manual_review"
assert "parsing_error" not in json.dumps(validation)
```

Update the existing timeout test model to implement
`with_structured_output(..., method="json_mode", include_raw=True)` and raise
`TimeoutError` only from `ainvoke()`. Assert `validationErrorCode == "timeout"`,
`validationErrorPhase == "model_invoke"`, and no exception text is persisted.

Add three more bounded cases:

1. The V4 budget starts exhausted. Assert both the audit and
   `validationErrorCode` equal `model_call_budget_exhausted`, attempts are
   bounded, recovery is manual, and execution is denied.
2. The hard deadline has expired. Assert both the audit and
   `validationErrorCode` equal `hard_deadline_exceeded`, no provider call is
   made, recovery is manual, and execution is denied.
3. A schema-valid response has `status="invalid"`. Assert
   `validationOrigin="llm_semantic"`, `semanticValidationStatus="invalid"`,
   `validationErrorCategory="model_rejected"`, `manual_review`, and
   `executionPermitted=false` after `_policy_gate()`.

For parsing failure, place a unique string such as
`SENSITIVE_RAW_VALIDATOR_SENTINEL` in both the fake envelope's `raw` value and
the parsing exception. After invocation, load the persisted `llm_validator`
step with `list_steps()` and checkpoint with `list_checkpoints()`. Assert the
sentinel is absent from the returned update, step payload, and checkpoint
payload.

- [ ] **Step 3: Run the V4 tests and verify RED**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py -k "semantic_validator or validator_router" -o addopts='' -q -p no:cacheprovider
```

Expected: new structured-method and error-metadata assertions FAIL against the
free-form one-shot implementation; existing router cases remain PASS.

- [ ] **Step 4: Add the V4 budget-aware structured invocation adapter**

Add a private method which accepts the already configured structured invoker,
reserves the role budget, enforces hard deadline and Validator timeout, appends
one bounded audit, and re-raises invocation exceptions so the shared helper can
classify them:

```python
async def _invoke_v4_structured_model(
    self,
    runtime: _ModelRuntime,
    *,
    role: ModelRole,
    invoker: object,
    prompt: object,
) -> object:
    started_at = monotonic()
    if runtime.deadlines.hard_expired():
        runtime.audits.append(
            _model_call_audit_payload(
                role=role,
                attempt=runtime.budget.used,
                duration_ms=0,
                safe_error_code="hard_deadline_exceeded",
            )
        )
        raise SafeModelInvocationFailure(
            SafeModelFailure(
                code="hard_deadline_exceeded",
                phase="model_invoke",
                retryable=False,
            )
        )
    try:
        attempt = runtime.budget.reserve(role)
    except ModelCallBudgetExceeded:
        runtime.audits.append(
            _model_call_audit_payload(
                role=role,
                attempt=runtime.budget.used,
                duration_ms=0,
                safe_error_code="model_call_budget_exhausted",
            )
        )
        raise SafeModelInvocationFailure(
            SafeModelFailure(
                code="model_call_budget_exhausted",
                phase="model_invoke",
                retryable=False,
            )
        )
    remaining = max(
        0.001,
        (runtime.deadlines.hard_deadline_at - _now()).total_seconds(),
    )
    try:
        response = await asyncio.wait_for(
            cast(Any, invoker).ainvoke(prompt),
            timeout=min(float(ROLE_TIMEOUT_SECONDS[role]), remaining),
        )
    except Exception as exc:
        runtime.audits.append(
            _model_call_audit_payload(
                role=role,
                attempt=attempt,
                duration_ms=int(round(elapsed_ms(started_at))),
                safe_error_code=_safe_model_call_error_code(exc),
            )
        )
        raise
    runtime.audits.append(
        _model_call_audit_payload(
            role=role,
            attempt=attempt,
            duration_ms=int(round(elapsed_ms(started_at))),
            safe_error_code=None,
        )
    )
    return response
```

Keep `_invoke_v4_model()` unchanged to avoid altering unrelated model roles.
Import the safe exception and metadata dataclass from `decision_validation.py`;
they carry no exception string or provider data.

- [ ] **Step 5: Replace the V4 free-form Validator implementation**

Initialize bounded metadata before candidate handling, use a closure to connect
the shared helper to the new adapter, and build the additive payload:

```python
validation_model = _validator_model_name(self._llm_provider)
outcome = None
if candidate is not None:
    async def invoke_validator(invoker: object, current_prompt: object) -> object:
        return await self._invoke_v4_structured_model(
            model_runtime,
            role="validator",
            invoker=invoker,
            prompt=current_prompt,
        )

    outcome = await invoke_structured_root_cause_validation(
        model=_validator_chat_model(self._llm_provider),
        prompt=prompt,
        available_evidence_ids=evidence_ids,
        structured_output_method=_validator_structured_output_method(
            self._llm_provider
        ),
        invoke=invoke_validator,
    )
    validation = outcome.decision
```

Set payload fields from `outcome` when present, otherwise use bounded
`candidate_missing` defaults:

```python
"validationOrigin": "llm_semantic" if validation is not None else "llm_failed",
"semanticValidationStatus": validation.status if validation is not None else "failed",
"semanticValidationAttempts": outcome.attempts if outcome is not None else 0,
"validationModel": validation_model,
"validationErrorCategory": (
    outcome.error_category if outcome is not None else "candidate_missing"
),
"validationErrorCode": outcome.error_code if outcome is not None else None,
"validationErrorCodes": list(outcome.error_codes) if outcome is not None else [],
"validationErrorPhase": outcome.error_phase if outcome is not None else None,
"validationRetryable": outcome.retryable if outcome is not None else None,
"validationHttpStatusClass": (
    outcome.http_status_class if outcome is not None else None
),
```

Retain the current recovery fallback whenever semantic validation is not valid.
Do not replace the deterministic `status` already copied into the payload.

- [ ] **Step 6: Run focused V4 tests and verify GREEN**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py -k "semantic_validator or validator_router" -o addopts='' -q -p no:cacheprovider
uv run pytest tests/test_aiops_validator_routing.py -o addopts='' -q -p no:cacheprovider
```

Expected: all selected tests PASS. In particular, proposal-only routing spends
no Validator call and execution-request routing still selects the Validator.

- [ ] **Step 7: Commit the V4 integration**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_v4_workflow.py
git commit -m "fix: use structured validation in v4"
```

### Task 3: Focused static verification and real canary

**Files:**
- Verify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Verify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Verify: `apps/backend/tests/test_aiops_decision_validation.py`
- Verify: `apps/backend/tests/test_aiops_v4_workflow.py`
- Modify after successful persistence: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: the implemented structured V4 Validator, existing Live CLI, active PostgreSQL knowledge base, CLS configuration, Docker scenario, and current dedicated Validator configuration.
- Produces: focused verification evidence and one immutable Multi canary record with independent cleanup evidence.

- [ ] **Step 1: Run focused regression tests**

```powershell
uv run pytest tests/test_aiops_decision_validation.py tests/test_aiops_validator_routing.py -o addopts='' -q -p no:cacheprovider
uv run pytest tests/test_aiops_v4_workflow.py -k "semantic_validator or validator_router" -o addopts='' -q -p no:cacheprovider
```

Expected: all selected tests PASS with no warnings or collection errors.

- [ ] **Step 2: Run Ruff and Pyright on touched modules only**

```powershell
uv run ruff check src/super_ai/aiops/decision_validation.py src/super_ai/aiops/diagnostics.py tests/test_aiops_decision_validation.py tests/test_aiops_v4_workflow.py
uv run pyright src/super_ai/aiops/decision_validation.py src/super_ai/aiops/diagnostics.py tests/test_aiops_decision_validation.py tests/test_aiops_v4_workflow.py
```

Expected: both commands exit 0. Do not substitute a full-project pytest run.

- [ ] **Step 3: Run one uniquely named real Multi canary**

From `apps/backend`, use the already configured non-secret project file and a
new immutable run ID:

```powershell
$validatorRunId = "v4-structured-validator-multi-" + (Get-Date -Format "yyyyMMddHHmmss")
$validatorCampaignId = "v4-structured-validator-20260821"
$validatorConfig = (Resolve-Path '..\..\..\..\config\project.json').Path
uv run python scripts/run_live_benchmark.py run --scenario APY-LIVE-ORDER-POOL-LEAK-001 --run-id $validatorRunId --owner-user-id user_c88807ff36b74a038b9e1ea31a389cfc --knowledge-base-id kb_user_c88807ff36b74a038b9e1ea31a389cfc --evidence-source cls --strategy multi --campaign-id $validatorCampaignId --config $validatorConfig
```

Expected: terminal result is persisted and cleanup succeeds. Inspect bounded
result/step data for `validationRequired=true`, reason
`execution_requested`, `validationModel`, structured attempt count, safe error
fields, recovery mode, score, and `executionPermitted`. Do not print the API
key, prompt, raw response, raw CLS records, or ground truth.

- [ ] **Step 4: Independently verify cleanup even if the canary fails**

```powershell
uv run python scripts/run_live_benchmark.py verify --scenario APY-LIVE-ORDER-POOL-LEAK-001 --run-id $validatorRunId
uv run python scripts/run_live_benchmark.py cleanup --scenario APY-LIVE-ORDER-POOL-LEAK-001 --run-id $validatorRunId
```

Expected: no run-scoped fault resources remain. If model validation still
fails, the persisted classification must distinguish setup, invocation, and
structured parsing failure; recovery must remain manual and execution denied.

- [ ] **Step 5: Record bounded acceptance evidence**

Append only the unique run ID, Git SHA, score/status, Validator model identifier,
semantic status, attempt count, safe failure classification if any, recovery
mode, verification result, cleanup result, elapsed time, and artifact location
to `docs/aiops/agentpy-domainbench.md`.

```powershell
git add docs/aiops/agentpy-domainbench.md
git commit -m "docs: record structured validator canary"
```

- [ ] **Step 6: Final worktree verification**

```powershell
git diff --check
git status --short --branch
git log -5 --oneline
```

Expected: no uncommitted implementation changes remain. No push, PR, merge, or
full pytest run is part of this plan.

## Acceptance Checklist

- [ ] V4 uses `with_structured_output()` with the configured Validator method.
- [ ] One format-only retry is budgeted and audited; a third attempt is impossible.
- [ ] `llm_semantic / valid` is persisted on success.
- [ ] Timeout and retry-exhaustion failures have distinct safe metadata.
- [ ] Budget exhaustion and hard deadline have matching Validator/audit codes.
- [ ] A schema-valid semantic rejection is `llm_semantic / invalid`, not `llm_failed`.
- [ ] Semantic failure preserves diagnosis but forces manual review and denies execution.
- [ ] Proposal-only deterministic routing skips the LLM Validator.
- [ ] Focused pytest, Ruff, and Pyright checks pass.
- [ ] One unique real Multi canary is persisted and independently cleaned up.
- [ ] No raw model/provider/CLS/secret/oracle content is persisted or printed.
- [ ] Sensitive sentinels in structured `raw` and parsing exceptions are absent
  from return values, persisted steps, and checkpoints.

## Self-Review

- Spec coverage: optional routing, provider method selection, shared helper reuse,
  budget/deadline/audit retention, one retry, bounded metadata, fail-closed
  recovery, semantic rejection, runtime exhaustion classification, redaction
  across return/step/checkpoint, focused tests, real persistence, and cleanup
  all map to explicit tasks.
- Placeholder scan: no TBD, TODO, deferred implementation, or unspecified code
  step remains.
- Type consistency: the callback consumes the same configured invoker used by
  the helper; the V4 adapter returns `object`; payload names match the design
  document and existing evaluation artifact vocabulary.
