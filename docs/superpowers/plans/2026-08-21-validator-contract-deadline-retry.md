# Validator Contract and Deadline-Aware Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the optional V4 LLM Validator produce a more reliable schema-valid first response and skip its single correction attempt when the persisted hard deadline cannot provide a complete 60-second Validator window.

**Architecture:** Reuse the existing LangChain structured invoker, Pydantic schema, V4 model budget, deadline, and audit adapter. Add one shared public-output contract string and one optional synchronous retry guard to the structured Validator helper; the V4 workflow supplies a guard computed from its persisted hard deadline. Failures remain secret-safe and force manual review.

**Tech Stack:** Python 3.10, Pydantic v2, LangChain `with_structured_output`, asyncio, pytest, Ruff, strict Pyright, OpenSpec.

## Global Constraints

- Keep `validator_router_v4` conditional and deterministic; do not make the LLM Validator mandatory.
- Keep `_RootCauseValidationSchema` strict with `extra="forbid"`; do not relax enums, evidence allowlisting, or recovery policy.
- Do not persist prompts, raw responses, exception text, credentials, ground truth, Oracle data, private reasoning, or raw CLS logs.
- Do not add dependencies, change the configured model, extend the six-minute global hard deadline, or shorten Specialist evidence collection.
- A skipped correction attempt must not reserve or consume a model-call budget unit.
- Run focused tests only; do not run the full pytest suite.

---

### Task 1: Add a safe deadline-aware correction guard

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Test: `apps/backend/tests/test_aiops_decision_validation.py`

**Interfaces:**
- Produces: `StructuredValidationRetryGuard = Callable[[], bool]`.
- Extends: `invoke_structured_root_cause_validation(..., allow_format_retry: StructuredValidationRetryGuard | None = None)`.
- Produces: `ValidationErrorCode` member `retry_skipped_insufficient_deadline`.
- Preserves: all current callers; `None` continues to allow the existing one correction attempt.

- [ ] **Step 1: Write the failing helper test**

Add a test using a structured model whose first envelope contains a safe parse failure and whose second envelope is valid. Pass `allow_format_retry=lambda: False` and assert:

```python
assert outcome.decision is None
assert outcome.error_category == "retry_exhausted"
assert outcome.error_code == "retry_skipped_insufficient_deadline"
assert outcome.error_codes == (
    "structured_envelope_mismatch",
    "retry_skipped_insufficient_deadline",
)
assert outcome.error_phase == "structured_parse"
assert outcome.retryable is False
assert outcome.attempts == 1
assert model.structured.calls == 1
```

Also keep the existing callback retry-success test proving that a missing guard still allows exactly two attempts.

- [ ] **Step 2: Run the new test and verify RED**

Run from `apps/backend`:

```powershell
uv run pytest tests/test_aiops_decision_validation.py -k "insufficient_deadline or callback_for_format_retry" -q
```

Expected: the new test fails because `allow_format_retry` and the safe skip code do not exist; the existing retry test passes.

- [ ] **Step 3: Implement the minimal helper behavior**

In `decision_validation.py`:

```python
StructuredValidationRetryGuard = Callable[[], bool]
```

Add `retry_skipped_insufficient_deadline` to `ValidationErrorCode`. Add the optional `allow_format_retry` parameter. Immediately after recording the first parse code, consult the guard before changing the prompt or continuing. If it returns false, return a `StructuredValidationOutcome` with one attempt, `retry_exhausted`, `structured_parse`, `retryable=False`, the skip code as `error_code`, and a bounded deduplicated `error_codes` tuple containing the original parse codes followed by the skip code. Do not call the invocation callback and do not catch or persist guard-specific data.

- [ ] **Step 4: Run the focused helper tests and verify GREEN**

```powershell
uv run pytest tests/test_aiops_decision_validation.py -k "structured_validator" -q
```

Expected: all selected Validator tests pass, including existing parse taxonomy and two-attempt behavior.

### Task 2: Share an exact output contract and bind the retry guard to V4 deadlines

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/decision_validation.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Test: `apps/backend/tests/test_aiops_v4_workflow.py`

**Interfaces:**
- Produces: `ROOT_CAUSE_VALIDATION_OUTPUT_CONTRACT: str`, reused by the initial V4 prompt and correction suffix.
- Consumes: `ROLE_TIMEOUT_SECONDS["validator"] == 60` and `model_runtime.deadlines.hard_deadline_at`.
- Preserves: `_invoke_v4_structured_model()` as the only path that reserves budget, applies timeout, invokes the provider, and appends model-call audits.

- [ ] **Step 1: Write failing V4 prompt and deadline tests**

Extend `test_v4_semantic_validator_uses_configured_json_mode` to inspect the first captured prompt and assert it contains:

```python
assert '"status": "valid"' in prompt
assert '"evidenceIds": ["evidence-id"]' in prompt
assert '"unsupportedFields": []' in prompt
assert '"missingEvidence": []' in prompt
assert "valid or invalid" in prompt
assert "No additional fields" in prompt
```

Add a V4 test with a first parse failure and deadlines whose hard deadline is less than 60 seconds in the future. Assert one captured prompt, one new audit, `model_call_count` increased by one only, `semanticValidationAttempts == 1`, both safe error codes are persisted, recovery is `manual_review`, and Policy Gate returns `executionPermitted=false`.

- [ ] **Step 2: Run the new V4 tests and verify RED**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py -k "semantic_validator and (json_mode or insufficient_deadline)" -q
```

Expected: prompt assertions and insufficient-deadline assertions fail against the current short prompt and unconditional retry.

- [ ] **Step 3: Implement the shared contract and V4 guard**

Define one compact synthetic output contract next to `_RootCauseValidationSchema` and build `_CORRECTION_SUFFIX` from it. It must name exactly the five camelCase fields, state their JSON types, constrain `status` and `unsupportedFields`, require non-empty `summary`, require empty arrays when there are no items, prohibit additional fields, and include the synthetic example from the approved design.

Import and prepend/append this contract in `_llm_validator_v4()` before public candidate and observation data. Supply:

```python
def allow_format_retry() -> bool:
    remaining = (model_runtime.deadlines.hard_deadline_at - _now()).total_seconds()
    return remaining >= float(ROLE_TIMEOUT_SECONDS["validator"])
```

to the structured helper. Do not reserve budget in the guard and do not alter `_invoke_v4_structured_model()`.

- [ ] **Step 4: Run focused V4 and helper regressions and verify GREEN**

```powershell
uv run pytest tests/test_aiops_decision_validation.py -k "structured_validator" -q
uv run pytest tests/test_aiops_v4_workflow.py -k "semantic_validator or validator_router" -q
```

Expected: all selected tests pass; normal correction still uses two attempts, while insufficient time uses one.

### Task 3: Project the safe skip code into artifacts and complete verification

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Test: `apps/backend/tests/test_evaluation_artifacts.py`
- Modify: `openspec/changes/harden-aiops-decision-validation/tasks.md`
- Modify: `docs/aiops/agentpy-domainbench.md` only if the persisted field description currently enumerates error codes.

**Interfaces:**
- Extends: `_VALIDATION_ERROR_CODES` with `retry_skipped_insufficient_deadline`.
- Preserves: artifact scoring and all historical artifact parsing.

- [ ] **Step 1: Write the failing artifact allowlist test**

Add the new safe code to a validation step fixture and assert `artifact.validation_audit.error_codes` retains it together with the first parse code, while the existing unknown-code test still drops unknown values.

- [ ] **Step 2: Run the artifact test and verify RED**

```powershell
uv run pytest tests/test_evaluation_artifacts.py -k "validation_audit" -q
```

Expected: the new safe code is currently filtered out.

- [ ] **Step 3: Add the code to the artifact allowlist and close OpenSpec tasks**

Add only `retry_skipped_insufficient_deadline` to `_VALIDATION_ERROR_CODES`; do not accept arbitrary strings and do not change scoring. After all focused checks pass, mark tasks 7.1 through 7.3 complete. Update DomainBench only if its current error-code description would otherwise become inaccurate.

- [ ] **Step 4: Run focused verification**

From `apps/backend`:

```powershell
uv run pytest tests/test_aiops_decision_validation.py -k "structured_validator" -q
uv run pytest tests/test_aiops_v4_workflow.py -k "semantic_validator or validator_router" -q
uv run pytest tests/test_evaluation_artifacts.py -k "validation_audit" -q
uv run ruff check src/super_ai/aiops/decision_validation.py src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/artifacts.py tests/test_aiops_decision_validation.py tests/test_aiops_v4_workflow.py tests/test_evaluation_artifacts.py
uv run pyright src/super_ai/aiops/decision_validation.py src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/artifacts.py tests/test_aiops_decision_validation.py tests/test_aiops_v4_workflow.py tests/test_evaluation_artifacts.py
```

From the repository root:

```powershell
openspec validate harden-aiops-decision-validation --strict
git diff --check
```

Expected: all commands pass with no full pytest run and no live provider call.

- [ ] **Step 5: Record the implementation commit**

Stage only the files listed in this plan and commit with:

```powershell
git commit -m "fix: make validator correction deadline aware"
```
