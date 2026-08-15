# Grounded Causal-Chain Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve an otherwise valid LLM root-cause decision by replacing only a structurally invalid one-item causal chain with two to six persisted, evidence-linked Observation summaries.

**Architecture:** Add one pure repair helper beside the existing grounded fallback builder. The Decision node invokes it after parsing and canonical-label normalization but before persistence; a successful repair receives an explicit audit origin and still passes through the existing deterministic and LLM Decision validators. Failed repair retains the current bounded invalid/replan path.

**Tech Stack:** Python 3.10+, existing immutable reasoning dataclasses, LangGraph AIOps workflow, PostgreSQL-backed test repositories, pytest/pytest-asyncio, Ruff, strict Pyright; no new dependency.

## Global Constraints

- Do not modify ground truth, scoring, thresholds, answers, evidence requirements, or canonical labels.
- Repair only when the exact deterministic gap set is `{"causalChain"}`.
- Preserve the LLM component, mechanism, trigger, evidence IDs, and confidence exactly.
- Use only persisted public hypothesis state and Observation summaries linked to the decision's evidence IDs.
- Require exactly one strongly supported candidate and two to six non-empty grounded summaries.
- Preserve both existing Decision validators and fail closed when repair is unsafe.
- Do not call an evidence tool or additional model for the repair itself.
- Add no dependency or external service.
- Do not run another paid acceptance in this plan.

## File Structure

- Modify `apps/backend/src/super_ai/aiops/diagnostics.py`: add the pure repair helper and invoke it in the Decision node.
- Modify `apps/backend/tests/test_aiops_reasoning_trace.py`: cover safe repair, fail-closed cases, audit origin, and the complete APY-013 no-Replan path.

---

### Task 1: Pure grounded causal-chain repair

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `_repair_grounded_causal_chain(decision: RootCauseDecision, *, public_hypotheses: Sequence[JsonDict], hypothesis_states: Sequence[JsonDict], observation_decisions: Sequence[JsonDict], decision_vocabulary: JsonDict) -> RootCauseDecision | None`.
- Reuses: `build_grounded_fallback_decision()` and `_deterministic_decision_gaps()`.

- [ ] **Step 1: Write the failing successful-repair test**

Import the private helper with the existing Pyright private-usage suppression. Construct an LLM
decision with canonical labels, three persisted evidence IDs, and one causal-chain item. Provide one
supported `postgres_deadlock` state at confidence `1.0`, public hypothesis description, the existing
candidate-wide vocabulary, and three Observation decisions linked to the three evidence IDs.

```python
repaired = _repair_grounded_causal_chain(
    RootCauseDecision(
        component="order-service",
        mechanism="opposite_order_transaction_deadlock",
        trigger="Transactions acquired resources in opposite orders.",
        causal_chain=("One combined narrative.",),
        evidence_ids=("ev-error", "ev-cycle", "ev-order"),
        confidence=0.97,
    ),
    public_hypotheses=[
        {"id": "postgres_deadlock", "description": "Concurrent transactions formed a cycle."}
    ],
    hypothesis_states=[{
        "id": "postgres_deadlock",
        "status": "supported",
        "confidence": 1.0,
        "evidenceIds": ["ev-error", "ev-cycle", "ev-order"],
    }],
    observation_decisions=[
        {"supports": ["postgres_deadlock"], "evidenceIds": ["ev-error"], "summary": "PostgreSQL emitted SQLSTATE 40P01."},
        {"supports": ["postgres_deadlock"], "evidenceIds": ["ev-cycle"], "summary": "The wait graph contained a two-session cycle."},
        {"supports": ["postgres_deadlock"], "evidenceIds": ["ev-order"], "summary": "Transactions acquired shared resources in opposite order."},
    ],
    decision_vocabulary={
        "labelsByHypothesis": {
            "postgres_deadlock": {
                "component": "order-service",
                "mechanism": "opposite_order_transaction_deadlock",
            }
        }
    },
)
assert repaired is not None
assert repaired.causal_chain == (
    "PostgreSQL emitted SQLSTATE 40P01.",
    "The wait graph contained a two-session cycle.",
    "Transactions acquired shared resources in opposite order.",
)
```

Assert all five non-chain fields equal the original values exactly.

- [ ] **Step 2: Write fail-closed parameterized tests**

Starting from the Step 1 fixture, cover each independent rejection:

- a valid two-item original causal chain;
- a component or mechanism that does not match the supported candidate's public label;
- a second strongly supported hypothesis;
- only one linked Observation summary;
- a fallback evidence ID absent from the LLM decision;
- a decision with both `causalChain` and canonical-label gaps.

Every case must return `None`, leaving the caller to use the existing path.

- [ ] **Step 3: Run the helper tests and verify RED**

From `apps/backend`:

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py -q -k "grounded_causal_chain_repair" -p no:cacheprovider --basetemp=var/pytest-grounded-chain-red
```

Expected: collection fails because `_repair_grounded_causal_chain` does not exist.

- [ ] **Step 4: Implement the minimal pure helper**

The helper must first require:

```python
if set(_deterministic_decision_gaps(
    decision,
    decision_vocabulary=decision_vocabulary,
)) != {"causalChain"}:
    return None
```

Call `build_grounded_fallback_decision()` with the public state. Reject `None`, label mismatches,
fallback evidence not contained in `decision.evidence_ids`, and a chain length outside `2 <= n <= 6`.
Construct a new `RootCauseDecision` with every original field except `causal_chain`, then require a
second `_deterministic_decision_gaps()` call to return empty before returning it.

- [ ] **Step 5: Run helper tests and verify GREEN**

Run the Step 3 command again. Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "feat: repair causal chains from grounded observations"
```

---

### Task 2: Decision-node audit integration

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Changes: `_decision()` attempts the pure repair after LLM parsing/normalization.
- Persists: `decisionOrigin="llm_grounded_causal_chain_repair"` only on successful repair.

- [ ] **Step 1: Write the failing Decision-node test**

Use the existing PostgreSQL repository fixture and `AiopsDiagnosticService` to invoke `_decision()`
with three persisted evidence IDs, one strongly supported public candidate, three linked Observation
summaries, and public decision vocabulary. The scripted model returns canonical fields and a single
string-form `causalChain`.

Assert the returned/persisted decision has three Observation-derived chain items, preserves the
model's trigger/confidence/evidence IDs, and records:

```python
assert decision_step.payload["decisionOrigin"] == "llm_grounded_causal_chain_repair"
assert decision_step.payload["decisionErrorCategory"] is None
```

- [ ] **Step 2: Run the Decision-node test and verify RED**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py -q -k "decision_node_repairs_grounded_causal_chain" -p no:cacheprovider --basetemp=var/pytest-grounded-chain-node-red
```

Expected: the node persists `decisionOrigin="llm"` and the one-item chain.

- [ ] **Step 3: Integrate repair after canonical normalization**

Immediately after `normalize_root_cause_decision()`, call `_repair_grounded_causal_chain()` with the
current state's public hypotheses, hypothesis states, Observation decisions, and vocabulary. If it
returns a decision, replace the local decision and set the explicit repair origin. Do not catch or
mask unrelated parsing failures, and do not call the helper from the grounded-fallback branch.

- [ ] **Step 4: Run the Decision-node test and verify GREEN**

Run the Step 2 command again. Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "feat: audit grounded decision repairs"
```

---

### Task 3: APY-013 no-Replan regression and offline verification

**Files:**
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Verify: `apps/backend/src/super_ai/aiops/diagnostics.py`

**Interfaces:**
- Verifies the real failure shape through the production LangGraph workflow without a paid model.

- [ ] **Step 1: Change the scripted APY-013 model to return the real one-string shape**

In `PostgresContractAcceptanceChatModel`, replace the three-item Decision `causalChain` with:

```python
"causalChain": (
    "Transactions acquire shared resources in opposite orders -> "
    "a wait cycle forms -> PostgreSQL aborts one transaction."
),
```

Keep the existing errors → wait graph → resource-order plan and sufficient-after-two behavior.

- [ ] **Step 2: Strengthen the composed regression**

In `test_apy_013_sufficient_cycle_collects_three_relevant_exact_calls_and_a_decision`, assert:

- exactly three Snapshot observations and no `GetDatabaseMetrics`;
- exactly one `decision` and one `decision_validation` step;
- no `replanner` step whose reason is `decision_validation_gap`;
- Decision origin is `llm_grounded_causal_chain_repair`;
- repaired causal chain contains three Observation summaries;
- Decision validation is `valid` and the task succeeds.

- [ ] **Step 3: Run the composed APY-013 regression**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py::test_apy_013_sufficient_cycle_collects_three_relevant_exact_calls_and_a_decision -q -p no:cacheprovider --basetemp=var/pytest-apy013-grounded-chain
```

Expected: PASS without any real model or external evidence call.

- [ ] **Step 4: Run relevant offline regression**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_evaluation_scenarios.py tests/test_snapshot_benchmark_runner.py tests/test_snapshot_evaluation_tools.py tests/test_aiops_reasoning_trace.py tests/test_evaluation_scoring.py -q -p no:cacheprovider --basetemp=var/pytest-grounded-chain-suite
```

Expected: all selected tests pass.

- [ ] **Step 5: Run static checks**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m ruff check src tests
& .\.venv\Scripts\python.exe -m pyright
```

Expected: zero Ruff and Pyright errors.

- [ ] **Step 6: Run the ordinary offline suite once**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=var/pytest-grounded-chain-full
```

Expected: all non-live tests pass; only explicitly marked live tests may be skipped by project
configuration. Do not run `scripts/run_snapshot_benchmark.py` in this plan.

- [ ] **Step 7: Commit regression changes if not already committed**

```powershell
git add apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "test: prevent causal shape evidence replans"
```

If Task 1 or Task 2 already committed the final test content and the worktree is clean, do not
create an empty commit.
