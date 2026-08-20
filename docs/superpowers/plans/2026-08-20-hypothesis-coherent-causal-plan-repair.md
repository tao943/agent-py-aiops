# Hypothesis-Coherent Causal Plan Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent trigger, mechanism, and impact steps for different hypotheses from being accepted as one complete causal plan, then reuse trusted persisted Evidence to close a uniquely supported hypothesis without repeating tools.

**Architecture:** Keep `causal_intents.py` provider- and scenario-neutral by making causal coverage hypothesis-aware and placing public Live tool capabilities in one registry consumed by role validation, generic planning, and binding repair. Merge only known public hypothesis bindings into model plans, and project already persisted mechanism/impact observations only from explicit trusted facts/rules. The recovery policy, validators, scores, Evidence thresholds, and tool permissions remain unchanged.

**Tech Stack:** Python 3.10, LangGraph 1.2, Pydantic v2, pytest, Ruff, strict Pyright, PostgreSQL, Milvus, Tencent CLS, DashScope Qwen.

## Global Constraints

- Do not add a dependency or copy external source code.
- Do not read or expose `ground_truth.yaml`, primary cause, required Evidence, credentials, or raw private reasoning.
- Do not add scenario IDs or Oracle vocabulary to `super_ai.aiops.causal_intents`.
- Do not lower independent-positive-Evidence, causal-role, Validator, score, or recovery gates.
- Do not change `OrderPoolRecoveryService` authorization conditions.
- Do not run the full pytest suite.
- Keep `config/project.json` and `config/user.project.json` ignored and uncommitted.
- Preserve the failed Run `order-pool-q3r-ab-single-01-20260820`.

---

### Task 1: Reject cross-hypothesis causal coverage

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/causal_intents.py:129`
- Test: `apps/backend/tests/test_aiops_causal_intents.py:74`

**Interfaces:**
- Consumes: normalized `Sequence[JsonDict]` with `testsHypotheses`, `causalIntent`, and tool names.
- Produces: unchanged `repair_plan_causal_coverage(steps) -> PlanCausalCoverage` call signature, plus `target_hypothesis_id` and hypothesis-coherent `missing_roles` semantics.

- [ ] **Step 1: Write the failing regression test**

Add a test using the real failed shape: pool/context tests lifecycle, sessions/mechanism tests lock, reachability/impact tests unavailable, and log/trigger tests lifecycle. Assert `complete is False` and that the original plan is not relabeled into a false complete chain.

```python
def test_plan_coverage_rejects_roles_split_across_hypotheses() -> None:
    plan = (
        _hypothesis_step("pool", "InspectOrderPoolState", "context", ("lifecycle",)),
        _hypothesis_step("sessions", "InspectOrderDatabaseSessions", "mechanism", ("lock",)),
        _hypothesis_step("health", "VerifyOrderDatabaseReachability", "impact", ("unavailable",)),
        _hypothesis_step("logs", "SearchLog", "trigger", ("lifecycle",)),
    )
    result = repair_plan_causal_coverage(plan)
    assert result.complete is False
    assert result.target_hypothesis_id == "lifecycle"
    assert result.missing_roles == ("mechanism", "impact")
```

- [ ] **Step 2: Verify RED**

Run from `apps/backend`:

```powershell
uv run pytest tests/test_aiops_causal_intents.py::test_plan_coverage_rejects_roles_split_across_hypotheses -q -p no:cacheprovider
```

Expected: FAIL because the current function reports `complete=True`.

- [ ] **Step 3: Implement hypothesis-coherent candidate selection**

For each allowed role assignment, collect public hypothesis IDs from `testsHypotheses`. Accept the assignment only when one hypothesis has exactly one trigger and at least one mechanism and impact among steps that test that same hypothesis. If none is complete, select the nearest hypothesis and return its actual missing roles instead of global missing roles. Order by missing-role count, minimal role changes, existing role priority, then hypothesis ID.

```python
candidates: list[
    tuple[int, tuple[int, ...], str, tuple[CausalIntent, ...]]
] = []
for assignment in product(*allowed_by_step):
    for hypothesis_id in sorted(all_tested_hypotheses):
        roles = [
            role
            for step, role in zip(original, assignment, strict=True)
            if hypothesis_id in _string_set(step.get("testsHypotheses"))
        ]
        if roles.count("trigger") == 1 and "mechanism" in roles and "impact" in roles:
            candidates.append((changes, priority, hypothesis_id, typed_assignment))
```

- [ ] **Step 4: Verify GREEN and existing causal-intent regression**

```powershell
uv run pytest tests/test_aiops_causal_intents.py -q -p no:cacheprovider
```

Expected: all tests pass, including Oracle-isolation source scan.

- [ ] **Step 5: Commit**

```powershell
git add apps/backend/src/super_ai/aiops/causal_intents.py apps/backend/tests/test_aiops_causal_intents.py
git commit -m "fix: require hypothesis-coherent causal plans"
```

### Task 2: Centralize trusted Live capabilities and repair model bindings

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py:281`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py:5490`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py:4161`
- Test: `apps/backend/tests/test_live_diagnostic_adapter.py:474`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py:908`

**Interfaces:**
- Consumes: model plan, known public hypothesis IDs, and the existing `build_generic_live_plan()` public contract.
- Produces: one public capability registry shared by `allowed_causal_intents()`, `build_generic_live_plan()`, and `merge_trusted_live_hypothesis_bindings()`.

- [ ] **Step 1: Write failing binding tests**

Add one test showing that the failed model plan receives only hypotheses registered for the same tools. Assert lifecycle is added to sessions and reachability, unknown/empty/duplicate/nested values are removed, stable order is preserved, and `testsHypothesesOrigin` is recorded only for changed steps. Add consistency tests proving role validation, generic planning, and repair consume the same registry.

```python
repaired = merge_trusted_live_hypothesis_bindings(
    failed_plan,
    known_hypotheses=("lifecycle", "lock", "unavailable"),
)
assert "lifecycle" in repaired[1]["testsHypotheses"]
assert "lifecycle" in repaired[2]["testsHypotheses"]
assert repaired[1]["testsHypothesesOrigin"] == "trusted_capability_repair"
```

Add negative tests where `InspectFutureSubsystem` cannot gain model-omitted hypotheses, nested `oracle`/`primary_cause`-like fields cannot affect bindings, unchanged steps preserve their origin, and Create Plan/Replanner produce the same normalized binding result.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/test_live_diagnostic_adapter.py -q -p no:cacheprovider -k "generic_order_pool or trusted_live_hypothesis"
```

Expected: FAIL because the merge helper does not exist.

- [ ] **Step 3: Implement the merge helper**

Introduce an immutable public capability registry in `causal_intents.py`. Refactor `allowed_causal_intents()` and `build_generic_live_plan()` to read it, then have the merge helper read the same entries. Intersect model and trusted bindings with flat string `known_hypotheses`; union only for registered tools. Do not change tool arguments or causal roles in this helper.

```python
def merge_trusted_live_hypothesis_bindings(
    plan: Sequence[JsonDict], *, known_hypotheses: Sequence[str]
) -> list[JsonDict]:
    trusted = {
        str(step["tool"]): _string_list(step.get("testsHypotheses"))
        for step in build_generic_live_plan(
            available_tools=[str(item.get("tool") or "") for item in plan],
            known_hypotheses=known_hypotheses,
        )
    }
    ...
```

- [ ] **Step 4: Integrate after tool-contract normalization**

Call the helper in `_create_plan()` after `normalize_tool_plan_steps()` and before `repair_plan_causal_coverage()`. Apply the same normalization to parsed Replanner steps. Generic fallback remains unchanged because it already comes from the trusted contract.

- [ ] **Step 5: Verify GREEN**

```powershell
uv run pytest tests/test_live_diagnostic_adapter.py tests/test_aiops_reasoning_trace.py -q -p no:cacheprovider -k "generic_order_pool or trusted_live_hypothesis or causal_intent"
```

Expected: selected tests pass; model plans still use only discovered tools and known hypotheses.

- [ ] **Step 6: Commit**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_live_diagnostic_adapter.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "fix: repair trusted live hypothesis bindings"
```

### Task 3: Reuse persisted mechanism and impact Evidence after adjudication

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py:3293`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py:579`
- Test: `apps/backend/tests/test_aiops_v4_workflow.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Consumes: public observation decisions, accepted hypothesis assessments, normalized facts, and trusted `testsHypotheses` metadata.
- Produces: hypothesis-linked observations without new tool calls, Evidence IDs, or recovery actions.

- [ ] **Step 1: Write the failing projection test**

Use four observations matching the failed Run. The adjudicator directly cites the CLS trigger for the uniquely supported lifecycle hypothesis; Runtime mechanism and impact observations test the same hypothesis but were not directly cited. Assert the projection links mechanism and impact to the supported hypothesis, keeps context uncounted, and produces at least three roles over distinct persisted Evidence IDs.

```python
projected = _project_adjudicated_observations(
    observations=observations,
    assessments=(supported_lifecycle, *refuted_competitors),
    facts=facts,
)
coverage = supported_causal_coverage(
    hypothesis_states=[supported_state],
    observation_decisions=projected,
)
assert coverage.missing_roles == ()
```

Add negative assertions: no projection for unknown hypothesis, context-only observation, missing Evidence ID, or an observation that refutes the supported hypothesis.

- [ ] **Step 2: Verify RED**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py tests/test_aiops_reasoning_trace.py -q -p no:cacheprovider -k "converged_causal or adjudicated_observation"
```

Expected: FAIL because adjudicated projection currently links only Evidence IDs explicitly cited by the model.

- [ ] **Step 3: Persist public plan-binding metadata on observations**

In `_fact_adapter()`, add the current step's filtered `testsHypotheses` to each observation decision. This metadata is public, bounded, and already present in the persisted plan.

```python
"testsHypotheses": _unique_strings(
    [item for item in current_step.get("testsHypotheses", []) if isinstance(item, str)]
),
```

- [ ] **Step 4: Add bounded post-adjudication projection**

When exactly one hypothesis is supported, add that ID to a persisted observation's `supports` only when all are true:

- the observation tests that hypothesis;
- its role is `mechanism` or `impact`;
- it has persisted Evidence IDs and explicit structured public facts matching a registered trusted rule;
- it does not refute the supported hypothesis;
- the role is allowed by the source tool capability registry.

Never infer support from summary text or merely `not refuted`. Neutral/error/default observations fail closed. Never synthesize a trigger; the trigger must remain directly supported by adjudicated or trusted-rule Evidence. Record `assessmentSource=trusted_fact_projection` and keep the original Evidence IDs. If model adjudication is required, allow at most one bounded call and fail closed on any model error.

- [ ] **Step 5: Verify GREEN and no duplicate tool execution**

```powershell
uv run pytest tests/test_aiops_v4_workflow.py tests/test_aiops_reasoning_trace.py tests/test_aiops_multi_agent_runtime.py -q -p no:cacheprovider -k "converged_causal or adjudicated_observation or evidence or duplicate"
```

Expected: selected tests pass; model/tool call counts and Evidence IDs remain stable.

- [ ] **Step 6: Commit**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_v4_workflow.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "fix: reuse converged causal evidence"
```

### Task 4: Restore controlled model configuration

**Files:**
- Local only: `config/project.json`
- Local only: `config/user.project.json`

**Interfaces:**
- Consumes: existing ignored local configuration and API key.
- Produces: effective `chatModel=qwen3.7-plus`, `rerankModel=qwen3-vl-rerank` without changing validator, embedding, CLS, KB, or credentials.

- [ ] **Step 1: Patch only model values**

Set both effective local config layers to:

```json
{
  "chatModel": "qwen3.7-plus",
  "rerankModel": "qwen3-vl-rerank"
}
```

Do not modify `validatorModel`, `embeddingModel`, API key, endpoint, or CLS fields.

- [ ] **Step 2: Verify effective configuration safely**

Load the merged configuration and print only model names. Assert both files remain ignored with `git status --ignored` and do not stage them.

- [ ] **Step 3: Run target live model readiness**

```powershell
$env:AGENTPY_PROJECT_CONFIG='<main-workspace>\config\project.json'
uv run pytest tests/test_live_llm.py::test_live_chat_readiness tests/test_live_llm.py::test_live_rerank -q -p no:cacheprovider -m live_llm
```

Expected: 2 passed.

### Task 5: Targeted verification and one real Single canary

**Files:**
- Modify after real evidence only: `docs/aiops/agentpy-domainbench.md`
- Runtime only: ignored Evaluation Archive and `apps/backend/var/`

**Interfaces:**
- Consumes: fixed code, controlled models, active/indexed 30-card KB, healthy PostgreSQL/order-api, real CLS.
- Produces: one preserved Single canary Run and a decision whether a new 3×3 campaign may start.

- [ ] **Step 1: Run the complete targeted offline set**

```powershell
uv run pytest tests/test_aiops_causal_intents.py tests/test_aiops_reasoning_trace.py tests/test_aiops_v4_workflow.py tests/test_aiops_multi_agent_runtime.py tests/test_live_diagnostic_adapter.py tests/test_live_order_pool_contracts.py tests/test_live_evaluation_scoring.py tests/test_live_semantic_scoring.py -q -p no:cacheprovider
```

Expected: all selected tests pass; no full pytest run.

- [ ] **Step 2: Run static verification**

```powershell
uv run ruff check src/super_ai/aiops/causal_intents.py src/super_ai/aiops/diagnostics.py tests/test_aiops_causal_intents.py tests/test_aiops_reasoning_trace.py tests/test_aiops_v4_workflow.py tests/test_live_diagnostic_adapter.py
uv run pyright src/super_ai/aiops/causal_intents.py src/super_ai/aiops/diagnostics.py tests/test_aiops_causal_intents.py tests/test_aiops_reasoning_trace.py tests/test_aiops_v4_workflow.py tests/test_live_diagnostic_adapter.py
```

Expected: zero Ruff and Pyright errors.

- [ ] **Step 3: Audit real dependencies**

Run `audit_knowledge_index_scope.py` for the existing owner/KB and require 30 documents, 180 chunks, and zero scope mismatch. Require CLS SSE reachable and both PostgreSQL/order-api healthy. Query the old failed Run before the canary and record its immutable status, result identity, and cleanup terminal state for a post-run equality check.

- [ ] **Step 4: Run one new Single canary**

Generate and preflight a unique run ID and campaign, then use fixed Git SHA, `--evidence-source cls`, `--strategy single`, and the ignored main project config. Save every terminal state. After completion, assert the new Run exists in both PostgreSQL and Evaluation Archive and that persisted cleanup state is terminal. Re-query the old failed Run and assert the previously recorded fields are unchanged.

Success requires:

```text
VALID_PASS
requested strategy = single
effective strategy != multi_agent
verificationPassed = true
cleanupSucceeded = true
securityHardGatePassed = true
```

On any failure, run Verify/Cleanup immediately, preserve the failed record, and stop without starting 3×3 A/B.

- [ ] **Step 5: Record verified outcome and commit**

Update DomainBench with the exact Run ID, Git SHA, controlled models, safe metrics, failure/success classification, and cleanup status. Do not record credentials, raw CLS logs, Oracle labels, or private reasoning.

```powershell
git add docs/aiops/agentpy-domainbench.md
git commit -m "docs: record causal repair canary"
```

## Self-Review

- Spec coverage: hypothesis coherence, trusted binding repair, persisted Evidence reuse, fail-closed recovery, controlled models, targeted tests, and one canary are each mapped to a task.
- Placeholder scan: runtime IDs are intentionally generated at execution time and are not source-code placeholders; all code interfaces and commands are explicit.
- Type consistency: `repair_plan_causal_coverage()` call signature stays stable; `PlanCausalCoverage` gains a nullable target hypothesis, and the new registry/helper types are explicit and immutable.
- Scope: no UI, schema migration, dependency, API, evaluator threshold, recovery permission, or full A/B change is included.
