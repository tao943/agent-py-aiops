# Snapshot Decision and Evidence Refinement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Snapshot diagnostics expose equal canonical labels for every candidate, finish already-planned evidence that refines a supported hypothesis, and tolerate a string-form causal chain without changing benchmark answers or scoring.

**Architecture:** Extend the public scenario model with an explicit candidate decision label and derive the same `decisionVocabulary` contract already used by Live scenarios. Add one pure plan-selection helper to the existing Sufficiency Gate so it can skip irrelevant remaining steps and execute the first unexecuted step targeting a supported hypothesis. Keep root-cause parsing strict except for normalizing one non-empty `causalChain` string into a singleton tuple.

**Tech Stack:** Python 3.10+, dataclasses, PyYAML, existing LangGraph AIOps workflow, PostgreSQL test repositories, pytest/pytest-asyncio, Ruff, strict Pyright; no new dependency.

## Global Constraints

- Do not modify `ground_truth.yaml`, evaluator logic, score weights, pass thresholds, required evidence, or canonical correct answers.
- Do not expose evidence IDs, correct-hypothesis markers, answer priority, recovery answers, or oracle fields.
- Every public candidate hypothesis must have the same non-empty component/mechanism label structure.
- Refinement may execute only an existing, unexecuted plan step whose `testsHypotheses` intersects the supported set.
- Preserve exact Snapshot MCP matching, the six-attempt budget, answer isolation, audit persistence, and downstream deterministic Decision validation.
- Ordinary CI remains offline and requires no paid model, PostgreSQL service outside its existing test fixture, CLS, or Docker.
- Add no dependency or external service.
- Do not run a paid acceptance until all offline gates pass.

## File Structure

- Modify `apps/backend/src/super_ai/evaluation/domain.py`: add the immutable public decision-label value object and attach it to each `PublicHypothesis`.
- Modify `apps/backend/src/super_ai/evaluation/scenarios.py`: require and validate `decision_label` for every public hypothesis.
- Modify `apps/backend/src/super_ai/evaluation/runner.py`: derive candidate-wide `decisionVocabulary` and include it in Snapshot diagnostic input.
- Modify `apps/backend/src/super_ai/evaluation/__init__.py`: export the new public type alongside existing evaluation contracts.
- Modify all ten `benchmarks/agentpy/scenarios/APY-*/scenario.yaml` files: declare equal public labels for every candidate.
- Modify `apps/backend/src/super_ai/aiops/reasoning.py`: accept a non-empty string for `causalChain` as a singleton sequence.
- Modify `apps/backend/src/super_ai/aiops/diagnostics.py`: select and audit the first remaining supported-hypothesis refinement step.
- Modify `apps/backend/tests/test_evaluation_scenarios.py`: validate complete candidate labels and answer isolation.
- Modify `apps/backend/tests/test_snapshot_benchmark_runner.py`: validate the generated candidate-wide vocabulary.
- Modify `apps/backend/tests/test_aiops_reasoning_trace.py`: cover string parsing, pure refinement selection, Gate routing, and the offline `APY-013` evidence chain.

---

### Task 1: Public candidate decision labels and Snapshot vocabulary

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/domain.py`
- Modify: `apps/backend/src/super_ai/evaluation/scenarios.py`
- Modify: `apps/backend/src/super_ai/evaluation/runner.py`
- Modify: `apps/backend/src/super_ai/evaluation/__init__.py`
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`
- Modify: `apps/backend/tests/test_snapshot_benchmark_runner.py`
- Modify: `benchmarks/agentpy/scenarios/APY-002/scenario.yaml`
- Modify: `benchmarks/agentpy/scenarios/APY-003/scenario.yaml`
- Modify: `benchmarks/agentpy/scenarios/APY-006/scenario.yaml`
- Modify: `benchmarks/agentpy/scenarios/APY-007/scenario.yaml`
- Modify: `benchmarks/agentpy/scenarios/APY-011/scenario.yaml`
- Modify: `benchmarks/agentpy/scenarios/APY-012/scenario.yaml`
- Modify: `benchmarks/agentpy/scenarios/APY-013/scenario.yaml`
- Modify: `benchmarks/agentpy/scenarios/APY-014/scenario.yaml`
- Modify: `benchmarks/agentpy/scenarios/APY-015/scenario.yaml`
- Modify: `benchmarks/agentpy/scenarios/APY-016/scenario.yaml`

**Interfaces:**
- Produces: `PublicDecisionLabel(component: str, mechanism: str)`.
- Changes: `PublicHypothesis(id: str, description: str, decision_label: PublicDecisionLabel | None = None)`; the optional default preserves the shared Live-domain constructor, while the Snapshot loader always requires a label.
- Produces: `_snapshot_decision_vocabulary(scenario: PublicScenario) -> JsonDict` in `evaluation/runner.py`.
- Changes: `build_application_diagnostic_input()` always includes a candidate-wide `decisionVocabulary`.

- [ ] **Step 1: Write failing loader and vocabulary tests**

Add a loader assertion to `test_evaluation_scenarios.py`:

```python
def test_all_snapshot_candidates_declare_public_decision_labels() -> None:
    for scenario_dir in sorted(path for path in SCENARIOS.iterdir() if path.is_dir()):
        scenario = load_public_scenario(scenario_dir)
        assert scenario.hypotheses
        assert all(item.decision_label.component for item in scenario.hypotheses)
        assert all(item.decision_label.mechanism for item in scenario.hypotheses)
```

Create a temporary scenario from the existing fixture helper with no `decision_label`, then with
only `component`, and assert `load_public_scenario()` raises `ValueError` in both cases. Retain the
existing nested forbidden-key tests and add a case where `decision_label` contains `oracle` so the
current recursive leak detector rejects it.

In `test_snapshot_benchmark_runner.py`, extend the application-input test:

```python
scenario = load_public_scenario(SCENARIOS / "APY-013")
payload = build_application_diagnostic_input(scenario)
vocabulary = cast(dict[str, object], payload["decisionVocabulary"])
labels = cast(dict[str, dict[str, str]], vocabulary["labelsByHypothesis"])
assert set(labels) == {item.id for item in scenario.hypotheses}
assert labels["postgres_deadlock"] == {
    "component": "order-service",
    "mechanism": "opposite_order_transaction_deadlock",
}
assert "evidence" not in json.dumps(vocabulary).lower()
assert "oracle" not in json.dumps(vocabulary).lower()
```

Also assert every hypothesis ID maps to its own canonical mechanism in `mechanismAliases`, and
that `componentAliases` contains only canonical-component self mappings.

- [ ] **Step 2: Run the focused tests and verify RED**

Run from `apps/backend` with the worktree source explicitly first:

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_evaluation_scenarios.py tests/test_snapshot_benchmark_runner.py -q -p no:cacheprovider --basetemp=var/pytest-decision-label-red
```

Expected: failures show `PublicHypothesis` has no `decision_label`, missing labels are currently
accepted, and Snapshot input has no `decisionVocabulary`.

- [ ] **Step 3: Implement the immutable loader contract and vocabulary builder**

Add to `domain.py`:

```python
@dataclass(frozen=True, slots=True)
class PublicDecisionLabel:
    """Canonical public output labels for one candidate hypothesis."""

    component: str
    mechanism: str


@dataclass(frozen=True, slots=True)
class PublicHypothesis:
    id: str
    description: str
    decision_label: PublicDecisionLabel | None = None
```

In `scenarios.py`, bind each hypothesis through one mapping before construction and require both
label fields with the existing `_required_str` helper:

```python
hypothesis_payload = _as_mapping(item, "hypothesis")
label_payload = _required_mapping(hypothesis_payload, "decision_label")
PublicHypothesis(
    id=_required_str(hypothesis_payload, "id"),
    description=_required_str(hypothesis_payload, "description"),
    decision_label=PublicDecisionLabel(
        component=_required_str(label_payload, "component"),
        mechanism=_required_str(label_payload, "mechanism"),
    ),
)
```

Export `PublicDecisionLabel` from `evaluation/__init__.py`.

In `runner.py`, first fail with `ValueError("Snapshot hypotheses require decision labels.")` if any
shared-domain hypothesis has `decision_label is None`, then derive the vocabulary only from the
loaded public object:

```python
def _snapshot_decision_vocabulary(scenario: PublicScenario) -> JsonDict:
    labels = {
        item.id: {
            "component": item.decision_label.component,
            "mechanism": item.decision_label.mechanism,
        }
        for item in scenario.hypotheses
    }
    component_aliases = {
        item.decision_label.component: item.decision_label.component
        for item in scenario.hypotheses
    }
    mechanism_aliases = {
        alias: item.decision_label.mechanism
        for item in scenario.hypotheses
        for alias in (item.id, item.decision_label.mechanism)
    }
    return {
        "componentAliases": component_aliases,
        "mechanismAliases": mechanism_aliases,
        "labelsByHypothesis": labels,
    }
```

Include it as `"decisionVocabulary"` in `build_application_diagnostic_input()`. If two different
candidates try to bind the same alias to different mechanisms, raise `ValueError` rather than
silently overwrite; cover that helper with a direct unit test using a constructed scenario.

- [ ] **Step 4: Populate all candidate labels in public YAML**

Use this complete public mapping; every candidate gets exactly the same two-field structure:

| Scenarios | Hypothesis | Component | Mechanism |
|---|---|---|---|
| APY-002, APY-011 | `slow_database_work` | `postgresql` | `slow_transaction_pool_exhaustion` |
| APY-002, APY-011 | `application_connection_lifecycle` | `checkout-service` | `borrowed_connection_not_returned` |
| APY-002, APY-011 | `traffic_capacity` | `checkout-service` | `pool_capacity_exceeded_by_legitimate_concurrency` |
| APY-003, APY-006 | `upstream_process_down` | `checkout-service` | `process_unavailable` |
| APY-003, APY-006 | `upstream_port_mismatch` | `nginx-gateway` | `upstream_port_mismatch` |
| APY-003, APY-006 | `dns_resolution_failure` | `nginx-gateway` | `upstream_dns_resolution_failure` |
| APY-007, APY-012 | `redis_server_availability` | `redis` | `service_process_stopped` |
| APY-007, APY-012 | `redis_client_connection_lifecycle` | `checkout-service` | `stale_connections_retained_after_recovery` |
| APY-007, APY-012 | `redis_network_path` | `network` | `redis_network_path_failure` |
| APY-013 | `postgres_deadlock` | `order-service` | `opposite_order_transaction_deadlock` |
| APY-013 | `postgres_lock_wait` | `order-service` | `long_transaction_lock_blocking` |
| APY-013 | `postgres_slow_query` | `order-service` | `slow_query_timeout` |
| APY-014 | `redis_maxclients` | `live-eval-redis` | `benchmark_clients_exhausted_maxclients` |
| APY-014 | `redis_process_unavailable` | `live-eval-redis` | `connectivity_failure` |
| APY-014 | `host_file_descriptor_exhaustion` | `host` | `file_descriptor_exhaustion` |
| APY-014 | `redis_stale_client_pool` | `application` | `stale_client_pool` |
| APY-015 | `nginx_upstream_response_timeout` | `live-eval-upstream` | `upstream_response_exceeded_proxy_read_timeout` |
| APY-015 | `nginx_upstream_unavailable` | `live-eval-upstream` | `upstream_unavailable` |
| APY-015 | `nginx_route_mismatch` | `nginx` | `route_mismatch` |
| APY-015 | `nginx_gateway_pressure` | `nginx` | `gateway_resource_pressure` |
| APY-016 | `client_retry_storm` | `checkout-client` | `retry_after_ignored_without_backoff` |
| APY-016 | `expected_rate_limiting` | `api-gateway` | `expected_rate_limiting` |
| APY-016 | `malicious_traffic` | `edge` | `malicious_traffic` |
| APY-016 | `downstream_saturation` | `downstream` | `dependency_saturation` |

Do not read or modify any `ground_truth.yaml` in the implementation step.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the Step 2 command again. Expected: both files pass, and all ten checked-in scenarios load.

- [ ] **Step 6: Commit Task 1**

```powershell
git add apps/backend/src/super_ai/evaluation apps/backend/tests/test_evaluation_scenarios.py apps/backend/tests/test_snapshot_benchmark_runner.py benchmarks/agentpy/scenarios/*/scenario.yaml
git commit -m "feat: expose snapshot candidate decision labels"
```

---

### Task 2: Provider-compatible causal-chain parsing

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/reasoning.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `_string_sequence_or_singleton(payload: Mapping[str, object], field: str) -> tuple[str, ...]`.
- Changes: `parse_root_cause_decision()` uses it only for `causalChain`; other structured arrays remain strict.

- [ ] **Step 1: Write the failing parser tests**

Add tests beside the existing root-cause parser cases:

```python
def test_root_cause_decision_accepts_string_causal_chain_as_one_item() -> None:
    decision = parse_root_cause_decision(
        json.dumps({
            "component": "order-service",
            "mechanism": "opposite_order_transaction_deadlock",
            "trigger": "concurrent updates",
            "causalChain": "Concurrent transactions acquired resources in reverse order.",
            "evidenceIds": ["ev-deadlock", "ev-cycle"],
            "confidence": 1.0,
        }),
        available_evidence_ids={"ev-deadlock", "ev-cycle"},
    )
    assert decision.causal_chain == (
        "Concurrent transactions acquired resources in reverse order.",
    )
```

Parametrize `""`, whitespace-only text, an integer, a mapping, and a mixed-type list and assert
each raises `ValueError`. Keep the existing list-form success test unchanged.

- [ ] **Step 2: Run the parser tests and verify RED**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py -q -k "root_cause_decision" -p no:cacheprovider --basetemp=var/pytest-causal-chain-red
```

Expected: the new string case fails with `Model field 'causalChain' must be a sequence.`

- [ ] **Step 3: Add the narrow normalizer**

Implement a private helper that delegates list/tuple inputs to the existing `_string_tuple`, turns
one non-empty string into `(value.strip(),)`, and raises the same bounded `ValueError` for every
other type. Change only line 226's `causal_chain=` call; do not relax `evidenceIds`, observation
decisions, validation decisions, or recovery plans.

- [ ] **Step 4: Run the parser tests and verify GREEN**

Run the Step 2 command again. Expected: all selected parser tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add apps/backend/src/super_ai/aiops/reasoning.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "fix: normalize string causal chains"
```

---

### Task 3: Supported-hypothesis refinement routing

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `_supported_refinement_index(plan: Sequence[JsonDict], plan_index: int, supported_hypotheses: Collection[str], executed_fingerprints: Collection[str]) -> int | None`.
- Changes: `_sufficiency_gate()` may return `plan_index` for the selected existing step.
- Persists: `refinementReason` as either `supported_hypothesis_plan_step_remaining` or an empty string.

- [ ] **Step 1: Write failing pure selection tests**

Create three small plan steps using the real payload keys. Assert:

```python
assert _supported_refinement_index(
    plan=[metrics_step, resource_order_step],
    plan_index=0,
    supported_hypotheses={"postgres_deadlock"},
    executed_fingerprints=set(),
) == 1
```

The metrics step tests only `postgres_slow_query`; the resource-order step tests
`postgres_deadlock`. Add cases proving the helper returns `None` when only refuted/unresolved
candidate steps remain, skips an already-executed matching fingerprint, never examines indices
before `plan_index`, and returns the first of two valid refinement steps.

- [ ] **Step 2: Run the selection tests and verify RED**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py -q -k "supported_refinement" -p no:cacheprovider --basetemp=var/pytest-refinement-red
```

Expected: collection fails because `_supported_refinement_index` does not exist.

- [ ] **Step 3: Implement the pure selector**

The helper must follow this behavior without accessing an oracle or evidence milestone:

```python
def _supported_refinement_index(
    *,
    plan: Sequence[JsonDict],
    plan_index: int,
    supported_hypotheses: Collection[str],
    executed_fingerprints: Collection[str],
) -> int | None:
    supported = set(supported_hypotheses)
    executed = set(executed_fingerprints)
    if not supported:
        return None
    for index in range(max(plan_index, 0), len(plan)):
        step = plan[index]
        raw_tests = step.get("testsHypotheses")
        tested = {
            item
            for item in raw_tests
            if isinstance(item, str)
        } if isinstance(raw_tests, list) else set()
        if supported.isdisjoint(tested) or _step_fingerprint(step) in executed:
            continue
        return index
    return None
```

Import `Collection` from `collections.abc`. Do not inspect descriptions, tool names, evidence IDs,
or benchmark scenario IDs.

- [ ] **Step 4: Write failing Gate-routing tests**

Use the existing service test harness to invoke `_sufficiency_gate()` with a valid sufficient model
response and a plan where an irrelevant metrics step precedes a supported resource-order step.
Assert the returned update contains:

```python
assert update["next_route"] == "executor"
assert update["plan_index"] == 1
assert update["termination_reason"] == ""
```

Read the persisted `sufficiency_gate` step and assert `nextRoute == "executor"` and
`refinementReason == "supported_hypothesis_plan_step_remaining"`. Add sibling cases for no
matching step and exhausted attempt budget, both routing to Decision without a `plan_index`
override.

- [ ] **Step 5: Run the Gate tests and verify RED**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py -q -k "sufficiency and refinement" -p no:cacheprovider --basetemp=var/pytest-gate-refinement-red
```

Expected: the sufficient case routes directly to Decision and has no refinement audit reason.

- [ ] **Step 6: Integrate the selector into the Sufficiency Gate**

When `decision.status == "sufficient"` and executor budget remains, compute the candidate index
from `decision.supported_hypotheses`, the current suffix, and
`state["executed_step_fingerprints"]`. If present, route to Executor, clear the termination reason,
set the returned `plan_index`, and persist the bounded refinement reason. Otherwise preserve the
current `evidence_sufficient` Decision route. Insufficient behavior remains byte-for-byte
equivalent except for the added empty audit field.

- [ ] **Step 7: Run Task 3 tests and verify GREEN**

Run the Step 5 command, then the full `test_aiops_reasoning_trace.py`. Expected: all pass.

- [ ] **Step 8: Commit Task 3**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_reasoning_trace.py
git commit -m "feat: refine supported diagnostic hypotheses"
```

---

### Task 4: Offline APY-013 workflow regression and isolation gates

**Files:**
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Modify: `apps/backend/tests/test_snapshot_benchmark_runner.py`
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`

**Interfaces:**
- Verifies the production Snapshot adapter, public vocabulary, LangGraph route, exact MCP evidence, and Decision path together.

- [ ] **Step 1: Rewrite the scripted APY-013 regression to reproduce the real stop-too-early case**

Adjust `PostgresContractAcceptanceChatModel` so its initial plan order is:

1. `InspectPostgresErrors` targeting `postgres_deadlock`;
2. `InspectPostgresWaitGraph` targeting `postgres_deadlock` and `postgres_lock_wait`;
3. `GetDatabaseMetrics` targeting only `postgres_slow_query`;
4. `InspectTransactionResourceOrder` targeting `postgres_deadlock`.

Return `status=sufficient` immediately after the second observation, with
`supportedHypotheses=["postgres_deadlock"]` and both alternatives refuted. Return a string-form
`causalChain` once so parsing compatibility is exercised, while allowing existing strict Decision
validation/fallback behavior to remain visible.

Rename the regression to
`test_apy_013_sufficient_cycle_collects_three_relevant_exact_calls_and_a_decision` and expect only:

```python
assert [item.tool_name for item in snapshot.observations] == [
    "InspectPostgresErrors",
    "InspectPostgresWaitGraph",
    "InspectTransactionResourceOrder",
]
```

Assert `GetDatabaseMetrics` was skipped, the resource-order and wait-cycle evidence IDs both exist,
the refinement Gate step is persisted, no attempt-budget error exists, and a root-cause Decision is
produced. Build the task input with `build_application_diagnostic_input(scenario)` so this path also
uses the new public vocabulary.

- [ ] **Step 2: Run the composed APY-013 regression**

Tasks 1 through 3 have already observed RED independently for every production behavior. This step
is a composed regression over those units and therefore should pass without another production
change.

Run:

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py::test_apy_013_sufficient_cycle_collects_three_relevant_exact_calls_and_a_decision -q -p no:cacheprovider --basetemp=var/pytest-apy013-refinement-integration
```

Expected: PASS with three completed Snapshot observations and no metrics call. If it fails because
one approved behavior is not wired through the production adapter, first add a narrower failing
test for that missing boundary, apply the minimal fix, and rerun this composed regression.

- [ ] **Step 3: Run the offline relevant regression suite**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest tests/test_evaluation_scenarios.py tests/test_snapshot_benchmark_runner.py tests/test_snapshot_evaluation_tools.py tests/test_aiops_reasoning_trace.py tests/test_evaluation_scoring.py -q -p no:cacheprovider --basetemp=var/pytest-snapshot-refinement-suite
```

Expected: all selected tests pass, including answer-isolation and exact Snapshot contract tests.

- [ ] **Step 4: Commit Task 4**

```powershell
git add apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_snapshot_benchmark_runner.py apps/backend/tests/test_evaluation_scenarios.py
git commit -m "test: cover snapshot decision refinement"
```

---

### Task 5: Static verification and staged real acceptance

**Files:**
- Verify only unless a new failure requires a TDD regression and minimal fix.
- Write report: `apps/backend/var/benchmarks/APY-013-decision-refinement-real-worktree.json`

**Interfaces:**
- Consumes the existing base `config/project.json`, auto-merged `config/user.project.json`, PostgreSQL data, and active indexed 30-card knowledge base.
- Produces one real APY-013 report and, only after a pass, up to three sequential sample reports.

- [ ] **Step 1: Run formatting and strict typing**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m ruff check src tests
& .\.venv\Scripts\python.exe -m pyright
```

Expected: both commands exit zero. If either fails, write a focused regression where applicable,
apply the minimal correction, and repeat both commands.

- [ ] **Step 2: Run the full ordinary offline suite once**

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=var/pytest-snapshot-refinement-full
```

Expected: all non-live tests pass; only explicitly registered live markers may be deselected by the
project's default pytest configuration.

- [ ] **Step 3: Confirm real-run preconditions without calling the model**

Verify `PYTHONPATH` resolves to this worktree's `apps/backend/src`, configuration loads from base
`config/project.json` with the user overlay, PostgreSQL is reachable, and the configured benchmark
owner can see one active indexed knowledge base containing 30 knowledge cards. Assign that exact
database owner ID to `$EvalOwner` and its exact knowledge-base ID to `$EvalKnowledgeBase`. Do not
print API keys or connection passwords.

- [ ] **Step 4: Run real APY-013 exactly once**

Run from `apps/backend` after Step 3 establishes `$EvalOwner` and `$EvalKnowledgeBase`:

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
& .\.venv\Scripts\python.exe scripts/run_snapshot_benchmark.py --scenario APY-013 --suite-version evidence-v2 --runs 1 --adapter application --config ../../config/project.json --rag-mode on --owner-user-id $EvalOwner --knowledge-base-id $EvalKnowledgeBase --output var/benchmarks/APY-013-decision-refinement-real-worktree.json
```

Expected: existing score threshold passes, required evidence includes the error plus resource-order
and wait-cycle facts, Decision uses a public canonical label, and no forbidden L3 recovery executes.
If it fails, stop paid execution and diagnose only this report.

- [ ] **Step 5: Sample APY-014 through APY-016 only after APY-013 passes**

Run one scenario at a time with the same source/config discipline. Stop immediately on the first
repeatable code defect or infrastructure-invalid result. Do not rerun a scenario merely to seek a
higher stochastic score.

- [ ] **Step 6: Record verification state**

If verification required code changes, commit their tests and minimal fixes. If no files changed,
do not create an empty commit. Finish with `git status --short --branch` and record the exact test
counts and real report paths in the handoff.
