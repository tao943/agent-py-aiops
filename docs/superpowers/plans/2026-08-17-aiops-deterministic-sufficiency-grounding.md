# AIOps Deterministic Sufficiency and Grounded Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent LLM sufficiency output from closing persisted open hypotheses and safely normalize an otherwise grounded LLM decision to exact Observation text before the unchanged Validator runs.

**Architecture:** Add one pure projection that makes persisted Hypothesis State authoritative, then let the existing LangGraph sufficiency node select an unexecuted Plan step that tests an open competitor. Replace the narrow causal-chain repair with a gated decision normalizer that uses the existing deterministic Validator before and after normalization; no answer data, semantic similarity, dependency, or recovery permission is added.

**Tech Stack:** Python 3.12, LangGraph, Pydantic v2, pytest, Ruff, Pyright, OpenSpec.

## Global Constraints

- Work only on `feat/snapshot-tool-calling-contract` in the existing isolated worktree.
- PostgreSQL remains the sole persistent database; Redis remains in the project stack but is unchanged.
- Do not change Benchmark answers, weights, thresholds, public labels, or recovery authorization.
- Do not persist Prompt, raw LLM response, exception text, Ground Truth, Oracle, raw CLS logs, or credentials.
- Do not run full pytest and do not automatically rerun the real APY-013 Benchmark.
- Do not add dependencies; directly reuse the existing LangGraph/Pydantic implementation and treat HolmesGPT/PyRCA as reference-only findings.

## File Structure

- `apps/backend/src/super_ai/aiops/diagnostics.py`: owns deterministic sufficiency projection, workflow routing, and grounded decision normalization.
- `apps/backend/src/super_ai/aiops/decision_validation.py`: remains the source of truth for deterministic decision checks; only reused, not relaxed.
- `apps/backend/tests/test_aiops_reasoning_trace.py`: unit and application-flow regressions for state authority, routing, normalization, and APY-013.
- `apps/backend/tests/test_aiops_decision_validation.py`: unchanged-validator safety regressions.
- `openspec/changes/harden-aiops-decision-validation/specs/aiops-diagnosis-tasks/spec.md`: records authoritative state and normalization behavior.
- `openspec/changes/harden-aiops-decision-validation/tasks.md`: records implementation and verification completion.
- `docs/aiops/agentpy-domainbench.md`: records bounded offline acceptance, without claiming a new real run.

---

### Task 1: Authoritative Sufficiency projection

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `_project_evidence_sufficiency(*, model_decision: EvidenceSufficiencyDecision, hypothesis_states: Sequence[JsonDict], evidence_ids: Sequence[str]) -> EvidenceSufficiencyDecision`.
- Preserves: allowlisted model `missing_evidence`, `recommended_tools`, and `summary` as advice; replaces status and all three hypothesis lists from persisted state.

- [ ] **Step 1: Write failing pure projection tests**

Add tests showing that a model claiming `sufficient` with empty unresolved hypotheses is projected to:

```python
assert projected.status == "insufficient"
assert projected.supported_hypotheses == ("postgres_deadlock",)
assert projected.refuted_hypotheses == ()
assert projected.unresolved_hypotheses == (
    "postgres_lock_wait",
    "postgres_slow_query",
)
```

Add the inverse case where exactly one persisted hypothesis is supported and every competitor is refuted; assert `status == "sufficient"`. Add a model-call-failure test proving `_fallback_evidence_sufficiency` produces the same persisted classification rather than always returning insufficient.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py -k "authoritative_sufficiency or sufficiency_model_failure" -q
```

Expected: FAIL because the projection does not exist and fallback status is not derived from the persisted state.

- [ ] **Step 3: Implement the minimal pure projection**

Classify each non-empty persisted `id` by exact status (`supported`, `refuted`, otherwise `open`). Set sufficient only when `len(supported) == 1 and not unresolved`; otherwise set insufficient. Bound public ID/evidence lists with existing `_unique_strings` behavior. Keep model advice only when the projected result is insufficient; clear recommended tools when sufficient. Make `_fallback_evidence_sufficiency` construct safe fallback advice and pass it through this same projection.

- [ ] **Step 4: Apply the projection after both model success and fallback**

In `_sufficiency_gate`, parse the model as today, then call `_project_evidence_sufficiency` before building the payload or routing. Ensure no raw model classification is persisted separately.

- [ ] **Step 5: Run Task 1 tests and verify GREEN**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py -k "authoritative_sufficiency or sufficiency_model_failure" -q
uv run ruff check src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py
```

Expected: PASS and Ruff clean.

### Task 2: Route open competitors to an executable Plan step

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `_next_open_hypothesis_step_index(*, plan: Sequence[JsonDict], plan_index: int, open_hypothesis_ids: Sequence[str], executed_fingerprints: Sequence[str]) -> int | None`.
- Uses: existing `_step_fingerprint`, `_can_replan`, `testsHypotheses`, `plan_index`, and `executor_attempt_count` contracts.

- [ ] **Step 1: Write failing routing tests**

Add a gate test with `postgres_deadlock=supported`, both competitors open, and remaining Metrics step testing `postgres_slow_query`. Assert:

```python
assert update["next_route"] == "executor"
assert update["plan_index"] == metrics_index
assert payload["refinementReason"] == "open_hypothesis_plan_step_remaining"
assert payload["unresolvedHypotheses"] == [
    "postgres_lock_wait",
    "postgres_slow_query",
]
```

Add cases proving an already executed matching step is skipped, no matching step routes to `replanner` while budget remains, and exhausted budget routes to Decision without changing open state so the Validator can fail closed.

- [ ] **Step 2: Run routing tests and verify RED**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py -k "open_hypothesis" -q
```

Expected: FAIL because current routing follows the LLM classification and sequential index.

- [ ] **Step 3: Implement deterministic competitor routing**

Scan unexecuted Plan entries from the current index onward, then earlier unexecuted entries if necessary. Select the first whose non-empty `testsHypotheses` intersects the projected unresolved set. Never synthesize a status change or alter tool arguments. In `_sufficiency_gate`, apply this route before causal-role refinement; after competitors are closed, retain the existing causal coverage route. If no matching step exists, use bounded Replan; on exhaustion retain fail-closed Decision behavior.

- [ ] **Step 4: Run Task 2 tests and verify GREEN**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py -k "sufficiency or open_hypothesis" -q
uv run ruff check src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py
```

Expected: PASS and Ruff clean.

### Task 3: Validator-gated grounded Decision normalization

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Test: `apps/backend/tests/test_aiops_decision_validation.py`

**Interfaces:**
- Replaces: `_repair_grounded_causal_chain(...)` with `_normalize_grounded_decision(...) -> RootCauseDecision | None`.
- Uses twice: `validate_grounded_candidate(...)`, first to identify expression-only failures and again to require all deterministic checks to pass.
- Persists: `decisionOrigin=llm_grounded_normalization` only when the normalized result passes every deterministic check.

- [ ] **Step 1: Write failing normalization tests**

Create a candidate with correct public component/mechanism and supporting Evidence IDs but paraphrased trigger/chain. Assert normalization copies the unique trigger Observation summary and produces ordered Observation summaries for `trigger -> mechanism -> context? -> impact`, while retaining component, mechanism, evidence IDs, and confidence.

Assert the pre-normalization failing checks are a non-empty subset of:

```python
{"trigger_present", "grounded_causal_chain"}
```

and the normalized result has no failed checks. Add fail-closed cases for an open competitor, wrong labels, outside/non-supporting Evidence, fewer than two positive Evidence IDs, fewer than two supporting Observations, missing or multiple trigger, and missing mechanism or impact.

- [ ] **Step 2: Run normalization tests and verify RED**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py tests/test_aiops_decision_validation.py -k "grounded_normalization or grounded_causal_chain_repair" -q
```

Expected: FAIL because the existing repair only handles its shallow `causalChain` gap and does not use Validator results.

- [ ] **Step 3: Implement all-or-nothing normalization**

Call `validate_grounded_candidate` on the original candidate with exactly the same public hypotheses, persisted state/evidence, observations, and vocabulary later used by the Decision Validator. Proceed only when every failed check is expression-only (`trigger_present` or `grounded_causal_chain`) and at least one failed. Build the canonical trigger/chain from `_grounded_observations` and `_grounded_trigger`; require a unique supported state, zero open states, matching normalized labels, candidate Evidence ownership, at least two positive Evidence IDs, at least two supporting Observations, and roles sufficient for a 2–6 item chain. Re-run `validate_grounded_candidate`; return the candidate only when all checks pass.

- [ ] **Step 4: Integrate origin without relaxing fallback**

Invoke normalization after a parsed LLM Decision and before LLM Validator. Rename the audit origin from `llm_grounded_causal_chain_repair` to `llm_grounded_normalization`. Keep `build_grounded_fallback_decision` and `deterministic_grounded_fallback` behavior unchanged for model failure, and do not turn a failed normalization into a fallback authorization.

- [ ] **Step 5: Run Task 3 tests and verify GREEN**

Run:

```powershell
uv run pytest tests/test_aiops_decision_validation.py tests/test_aiops_reasoning_trace.py -k "grounded or decision_node" -q
uv run ruff check src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py tests/test_aiops_decision_validation.py
```

Expected: PASS; existing strict Validator negative tests remain green.

### Task 4: APY-013 offline application-flow regression

**Files:**
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Updates: `PostgresContractAcceptanceChatModel` and APY-013 Snapshot contract expectations.
- Verifies: persisted Hypothesis State, four diagnostic calls, normalization origin, deterministic validation, manual/external recovery restrictions, and no Ground Truth access.

- [ ] **Step 1: Update the test fixture as RED**

Make the Sufficiency fake incorrectly claim all competitors refuted before Metrics runs. Keep the Metrics Observation as the actual refutation of `postgres_slow_query`; ensure WaitGraph refutes `postgres_lock_wait`. Change the expected call list to include exactly one `GetDatabaseMetrics`, with its existing normalized arguments and Evidence ID. Keep the root-cause fake paraphrased so the full flow requires deterministic normalization.

- [ ] **Step 2: Run the APY-013 regression and verify RED**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py::test_apy_013_sufficient_cycle_collects_three_relevant_exact_calls_and_a_decision -q
```

Expected: FAIL under pre-fix routing because Metrics is skipped or the Decision fails exact grounding.

- [ ] **Step 3: Update assertions to the new contract**

Assert four unique diagnostic calls, no tool error, both competitors persisted as refuted only after their Observations, one supported root cause, one Decision and one Validation, `decisionOrigin=llm_grounded_normalization`, deterministic checks all passing, `executionPermitted=false` for the application-level recovery, and no Replan when the remaining Plan step resolves the competitor.

- [ ] **Step 4: Run Task 4 test and verify GREEN**

Run:

```powershell
uv run pytest tests/test_aiops_reasoning_trace.py::test_apy_013_sufficient_cycle_collects_three_relevant_exact_calls_and_a_decision -q
```

Expected: PASS without network or LLM quota use.

### Task 5: Specification and bounded acceptance

**Files:**
- Modify: `openspec/changes/harden-aiops-decision-validation/specs/aiops-diagnosis-tasks/spec.md`
- Modify: `openspec/changes/harden-aiops-decision-validation/tasks.md`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Records: persisted-state authority, open-competitor routing, expression-only normalization, unchanged strict Validator, and offline verification evidence.

- [ ] **Step 1: Add OpenSpec scenarios**

Specify that model Sufficiency classification cannot override persisted Hypothesis State; a matching unexecuted Plan step is selected for open competitors; normalization is allowed only for exact expression check failures and must pass the original Validator afterward; unsafe inputs remain rejected.

- [ ] **Step 2: Run the two bounded pytest groups**

Run group A:

```powershell
uv run pytest tests/test_aiops_decision_validation.py tests/test_aiops_reasoning_trace.py -q
```

Run group B:

```powershell
uv run pytest tests/test_evaluation_scoring.py tests/test_evaluation_artifacts.py tests/test_evaluation_scenarios.py tests/test_evaluation_cli.py tests/test_snapshot_evaluation_tools.py tests/test_live_diagnostic_adapter.py -q
```

Expected: both groups PASS. Do not substitute full pytest.

- [ ] **Step 3: Run static and specification checks**

Run:

```powershell
uv run ruff check src/super_ai/aiops/diagnostics.py tests/test_aiops_reasoning_trace.py tests/test_aiops_decision_validation.py
uv run pyright
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate harden-aiops-decision-validation --strict
git diff --check
```

Expected: Ruff clean, Pyright `0 errors`, focused OpenSpec valid, and no whitespace errors.

- [ ] **Step 4: Record bounded offline acceptance**

Update DomainBench with changed behavior, exact scoped commands and outcomes. State explicitly that no new real APY-013 run was executed and therefore no production score claim is made.

- [ ] **Step 5: Audit repository hygiene**

Run:

```powershell
git status --short
git diff --stat
```

Expected: only intended source, tests, OpenSpec, plan/design, and DomainBench files are tracked; no `var/`, Archive, secret config, Prompt, raw response, exception detail, Ground Truth, Oracle, or raw CLS logs appear.

## Final Verification

- [ ] Every new behavior was introduced through a witnessed RED test followed by GREEN.
- [ ] The Validator check definitions, Benchmark score weights and recovery policy were not weakened.
- [ ] The two bounded pytest groups, Ruff, Pyright, focused OpenSpec strict, and `git diff --check` pass.
- [ ] No full pytest and no real APY-013 run were executed.
