# Docker Live Root-Cause Semantic Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace exact natural-language root-cause matching with deterministic evaluator-only semantic milestones so the grounded PostgreSQL Live baseline scores 100 without weakening structured-cause or safety checks.

**Architecture:** Extend the private Live oracle with immutable concept-rubric types, parse that rubric only at the evaluator boundary, and place normalization/matching in a focused `semantic_scoring.py` module. Keep component and mechanism strict after syntax normalization; award trigger and causal points only when both structured labels are correct.

**Tech Stack:** Python 3.12, frozen dataclasses, PyYAML, pytest, Ruff, Pyright; no new runtime dependencies, model calls, embeddings, or network calls.

## Global Constraints

- Preserve the existing 100-point total, recovery policy, and hard-gate behavior.
- Keep `root_cause_semantics` only in `ground_truth.yaml`; never include it in public scenarios, Agent inputs, Prompt, RAG, reports, or recovery policy.
- Component/mechanism comparison is deterministic canonical-label comparison, not fuzzy matching.
- Trigger and causal-chain matching uses only explicit evaluator-owned aliases.
- Semantic points require both component and mechanism to match.
- One causal milestone must be satisfied within one causal-chain step; never combine aliases across steps.
- Milestone order is irrelevant.
- Do not add LLM Judge, embeddings, network calls, dependencies, or external services.
- Execute inline in the current worktree; do not start subagents.

---

### Task 1: Private semantic-rubric contract and strict loader

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/domain.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/scenarios.py`
- Modify: `benchmarks/agentpy/live/APY-LIVE-PG-LOCK-001/ground_truth.yaml`
- Test: `apps/backend/tests/test_live_evaluation_scenarios.py`

**Interfaces:**
- Produces: `SemanticConcept(id: str, aliases: tuple[str, ...])`.
- Produces: `SemanticRequirement(id: str, all_of: tuple[str, ...])`.
- Produces: `RootCauseSemantics(concepts: tuple[SemanticConcept, ...], trigger: SemanticRequirement, causal_milestones: tuple[SemanticRequirement, ...])`.
- Extends: `ScenarioOracle.root_cause_semantics: RootCauseSemantics | None = None`, preserving shared Snapshot oracle construction.

- [ ] **Step 1: Write failing loader tests**

Add tests which copy the Live scenario to `tmp_path`, rewrite `ground_truth.yaml`, and assert:

```python
oracle = load_live_oracle(scenario_dir)
assert oracle.root_cause_semantics is not None
assert oracle.root_cause_semantics.trigger.all_of == ("lock_holder", "row_lock")
assert [item.id for item in oracle.root_cause_semantics.causal_milestones] == [
    "lock_held", "update_waits", "probe_times_out"
]
```

Parametrize invalid payloads for an unknown concept reference, an empty alias, duplicate milestone IDs, missing trigger, missing milestones, and missing `root_cause_semantics`; each must raise `ValueError` with a stable field-oriented message.

- [ ] **Step 2: Verify RED**

Run:

```powershell
uv run pytest tests/test_live_evaluation_scenarios.py -q
```

Expected: failures because `ScenarioOracle` has no semantic rubric and `load_live_oracle()` does not validate it.

- [ ] **Step 3: Add immutable domain types**

Add to `domain.py`:

```python
@dataclass(frozen=True, slots=True)
class SemanticConcept:
    id: str
    aliases: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class SemanticRequirement:
    id: str
    all_of: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class RootCauseSemantics:
    concepts: tuple[SemanticConcept, ...]
    trigger: SemanticRequirement
    causal_milestones: tuple[SemanticRequirement, ...]
```

Append `root_cause_semantics: RootCauseSemantics | None = None` to `ScenarioOracle` so all existing shared-oracle call sites remain compatible.

- [ ] **Step 4: Implement strict Live-only parsing**

Change `load_live_oracle()` to load the existing oracle, read private YAML via `_load_mapping`, and use `dataclasses.replace()` to attach `_root_cause_semantics(payload)`. The parser must:

```python
def _root_cause_semantics(payload: Mapping[str, object]) -> RootCauseSemantics: ...
def _semantic_requirement(
    payload: Mapping[str, object], label: str, *, default_id: str | None = None
) -> SemanticRequirement: ...
```

Validate non-empty concept mappings, non-empty/unique aliases, non-empty `all_of`, references to known concepts, exactly one trigger, at least one causal milestone, and unique non-empty milestone IDs. Error messages must name `root_cause_semantics`, `concepts`, `trigger`, or `causal_milestones` without serializing alias contents.

- [ ] **Step 5: Add the PostgreSQL evaluator-only rubric**

Append the approved `root_cause_semantics` mapping to `ground_truth.yaml`, defining `lock_holder`, `row_lock`, `waiter`, `wait_state`, `timeout`, and `causal_link`; configure trigger requirements and three milestones `lock_held`, `update_waits`, `probe_times_out`.

- [ ] **Step 6: Verify GREEN and commit**

Run:

```powershell
uv run pytest tests/test_live_evaluation_scenarios.py -q
uv run ruff check src/super_ai/evaluation/domain.py src/super_ai/evaluation/live/scenarios.py tests/test_live_evaluation_scenarios.py
```

Expected: all selected tests and Ruff pass.

Commit:

```powershell
git add apps/backend/src/super_ai/evaluation/domain.py apps/backend/src/super_ai/evaluation/live/scenarios.py apps/backend/tests/test_live_evaluation_scenarios.py benchmarks/agentpy/live/APY-LIVE-PG-LOCK-001/ground_truth.yaml
git commit -m "feat: load live semantic scoring rubric"
```

### Task 2: Deterministic semantic matcher

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/semantic_scoring.py`
- Create: `apps/backend/tests/test_live_semantic_scoring.py`

**Interfaces:**
- Consumes: `RootCauseSemantics`, `RootCauseDecision`.
- Produces: `RootCauseSemanticScore(component: int, mechanism: int, trigger: int, milestones: tuple[tuple[str, int], ...])` with `total` property.
- Produces: `score_root_cause_semantics(decision: RootCauseDecision | None, oracle: ScenarioOracle) -> RootCauseSemanticScore`.

- [ ] **Step 1: Write failing matcher tests**

Cover these independent behaviors with direct, real-function tests:

```python
assert score.total == 20  # exact oracle prose
assert score.total == 20  # grounded baseline-005 paraphrase
assert score.component == 4  # " PostgreSQL "
assert score.mechanism == 6  # "Row-Lock-Blocking"
assert score.trigger == 0 and sum(points for _, points in score.milestones) == 0
```

The last assertion must be exercised for wrong component and for mechanisms `deadlock`, `slow_query`, and `connectivity_failure`. Also test missing trigger concepts, each missing milestone, aliases split across separate causal steps, reordered milestones, punctuation/case normalization, and token boundaries that prevent substring matches.

- [ ] **Step 2: Verify RED**

Run:

```powershell
uv run pytest tests/test_live_semantic_scoring.py -q
```

Expected: collection fails because `semantic_scoring.py` does not exist.

- [ ] **Step 3: Implement syntax and text normalization**

Create private helpers:

```python
_LABEL_SEPARATOR = re.compile(r"[\s-]+")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)

def _normalize_label(value: str) -> str: ...
def _normalize_text(value: str) -> str: ...
def _contains_alias(normalized_text: str, alias: str) -> bool: ...
```

Use `casefold()`, normalize label separators to `_`, collapse repeated underscores, translate punctuation to spaces, collapse whitespace, and compare aliases as padded normalized token sequences rather than raw substrings.

- [ ] **Step 4: Implement milestone evaluation and score aggregation**

Build a concept lookup from the private rubric. A requirement matches only when every referenced concept has at least one alias present in the same input text. Award 4 for canonical component, 6 for canonical mechanism, 4 for trigger only after both structured matches, and 2 per matched milestone only after both structured matches. Raise `ValueError` if a Live oracle reaches the matcher without a semantic rubric.

- [ ] **Step 5: Verify GREEN, refactor, and commit**

Run:

```powershell
uv run pytest tests/test_live_semantic_scoring.py -q
uv run ruff check src/super_ai/evaluation/live/semantic_scoring.py tests/test_live_semantic_scoring.py
uv run pyright src/super_ai/evaluation/live/semantic_scoring.py tests/test_live_semantic_scoring.py
```

Expected: all selected checks pass.

Commit:

```powershell
git add apps/backend/src/super_ai/evaluation/live/semantic_scoring.py apps/backend/tests/test_live_semantic_scoring.py
git commit -m "feat: score live root cause semantics"
```

### Task 3: Integrate granular 4+6+4+6 scoring

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/scoring.py`
- Modify: `apps/backend/tests/test_live_evaluation_scoring.py`

**Interfaces:**
- Consumes: `score_root_cause_semantics()` from Task 2.
- Preserves: `_score_root_cause(...) -> int` and `LiveEvaluationResult` public shape.
- Emits reason codes: `primary_component_canonical`, `primary_mechanism_canonical`, `primary_trigger_semantic`, `causal_milestone_<id>`.

- [ ] **Step 1: Write failing integration tests**

Add a baseline paraphrase artifact using the persisted decision text:

```python
RootCauseDecision(
    component="postgresql",
    mechanism="row_lock_blocking",
    trigger="A transaction is holding a row lock required by order status updates.",
    causal_chain=(
        "The observation reveals a session waiting on a Lock event.",
        "The lock graph confirmed a blocker to waiter edge causing the timeouts.",
    ),
    evidence_ids=("ev-session", "ev-graph"),
    confidence=1.0,
)
```

Assert root cause is 20, total is 100, exact-oracle input still scores 100, and reason codes/maximums equal `4, 6, 4, 2, 2, 2`. Add incomplete and wrong-structured cases asserting detailed failure codes plus backward-compatible `primary_root_cause_wrong`.

- [ ] **Step 2: Verify RED**

Run:

```powershell
uv run pytest tests/test_live_evaluation_scoring.py -q
```

Expected: paraphrase remains 0/20 and granular reasons are absent.

- [ ] **Step 3: Replace exact scoring with semantic result mapping**

In `_score_root_cause`, call `score_root_cause_semantics()`, append one `ScoreReason` for each subscore, and add failures as follows:

```python
if semantic.component < 4: failures.append("primary_component_wrong")
if semantic.mechanism < 6: failures.append("primary_mechanism_wrong")
if semantic.trigger < 4: failures.append("primary_trigger_unsupported")
if any(points < 2 for _, points in semantic.milestones):
    failures.append("causal_chain_incomplete")
if semantic.total < 20: failures.append("primary_root_cause_wrong")
```

Reasons must contain only stable IDs and points; do not include oracle aliases or hidden prose.

- [ ] **Step 4: Verify GREEN and regression safety**

Run:

```powershell
uv run pytest tests/test_live_evaluation_scoring.py tests/test_live_semantic_scoring.py tests/test_live_evaluation_scenarios.py -q
```

Expected: all selected tests pass, total remains exactly 100, and existing hard-gate tests remain unchanged.

- [ ] **Step 5: Commit**

```powershell
git add apps/backend/src/super_ai/evaluation/live/scoring.py apps/backend/tests/test_live_evaluation_scoring.py
git commit -m "fix: score grounded live root cause semantics"
```

### Task 4: Isolation regression, persisted baseline re-score, and full verification

**Files:**
- Modify only if a regression is exposed: `apps/backend/tests/test_live_diagnostic_adapter.py`, `apps/backend/tests/test_snapshot_benchmark_runner.py`, or focused evaluator tests.
- Update plan checkboxes after evidence is captured: `docs/superpowers/plans/2026-08-14-live-root-cause-semantic-scoring.md`.

**Interfaces:**
- Verifies public scenario, diagnostic adapter, retrieval, report, and tools cannot observe `root_cause_semantics`.
- Verifies existing recovery and hard-gate contracts are unchanged.

- [ ] **Step 1: Run isolation and safety regressions**

Run:

```powershell
uv run pytest tests/test_live_diagnostic_adapter.py tests/test_snapshot_benchmark_runner.py tests/test_snapshot_evaluation_tools.py tests/test_knowledge_candidate_safety.py tests/test_evaluation_cli.py -q
```

Expected: all pass and no serialized Agent-facing structure contains `ground_truth`, `oracle`, `primary_cause`, or `root_cause_semantics`.

- [ ] **Step 2: Re-score baseline-005 without invoking the Agent or LLM**

Use the existing Live score/read-only CLI or repository query path for diagnostic task `diagnostic_8a51c1f40e7a431b97e9f8130b31eb59` and run ID `live-pg-lock-baseline-005`. Do not inject a fault or execute recovery. Expected result:

```text
root_cause: 20/20
total: 100/100
hard_gate: none
passed: true
```

If the persisted local row is unavailable, serialize the exact persisted decision in a deterministic regression test and report the environmental absence separately; do not spend LLM quota to recreate it.

- [ ] **Step 3: Run formatting, typing, and the full offline suite**

Run from `apps/backend`:

```powershell
uv run ruff check .
uv run pyright
uv run pytest -q
```

Expected: Ruff passes, Pyright reports zero errors, and the full offline suite has no failures.

- [ ] **Step 4: Inspect the diff for scope and oracle leakage**

Run:

```powershell
git diff origin/main...HEAD --check
git diff origin/main...HEAD --stat
rg -n "root_cause_semantics" apps/backend/src benchmarks apps/backend/tests
```

Expected: production references occur only in evaluator domain/Live loader/Live scorer; data appears only in private `ground_truth.yaml`; tests may reference it for isolation and scoring.

- [ ] **Step 5: Commit plan completion, push, and monitor CI**

```powershell
git add docs/superpowers/plans/2026-08-14-live-root-cause-semantic-scoring.md
git commit -m "docs: record semantic scoring verification"
git push origin fix/live-evidence-tool-args
```

Then verify PR #8 checks complete successfully. Do not merge without a separate user request.

## Execution Record

- Task 1 completed in commit `4cc5eef`; strict Live-only rubric loading tests passed.
- Task 2 completed in commit `570b270`; deterministic matcher, adversarial cases, Ruff, and focused Pyright passed.
- Task 3 completed in commit `b62e218`; the integrated 4+6+4+6 score and detailed failures passed 47 focused tests.
- Isolation/safety regression passed 62 tests.
- Historical task `diagnostic_8a51c1f40e7a431b97e9f8130b31eb59` was reconstructed read-only from PostgreSQL and re-scored at root cause 20/20, total 100/100, with no hard gate or failure.
- `uv run ruff check .` passed.
- `uv run pyright` passed with zero errors and warnings.
- `uv run pytest -q` passed under the project's default `not live_llm and not live_docker` selection; one expected test was skipped.
- An attempted custom marker selection `not integration and not live` overrode the project default and incorrectly invoked three real DashScope tests against the worktree's `offline-test-key`; those 401 results are not part of the offline verification.
