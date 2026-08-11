# AgentPy Benchmark Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working vertical slice of AgentPy DomainBench with two same-symptom/different-cause Snapshot scenarios, structured evidence and hypothesis updates, deterministic scoring, PostgreSQL result persistence, and a local CLI report.

**Architecture:** Keep benchmark ground truth outside the Agent runtime and expose frozen observations only through a typed Snapshot MCP client. Extend the existing LangGraph diagnosis workflow with bounded multi-tool planning, explicit hypothesis updates, and a structured final decision, then score the persisted run artifact with project-owned deterministic rules.

**Tech Stack:** Python 3.10+, FastAPI, LangGraph, SQLAlchemy 2, PostgreSQL 16, PyYAML, pytest, pytest-asyncio, existing MCP protocols; no new dependency.

## Global Constraints

- Work in `apps/backend` and use the existing strict Ruff and Pyright configuration.
- Do not add OpenSRE, Cloud-OpsBench, AIOpsLab, ITBench, or OpenRCA as a dependency.
- Do not expose `ground_truth.yaml` to the Agent, Snapshot MCP client, RAG, prompts, reports, or diagnostic API.
- Do not persist or display private Chain-of-Thought; persist only structured, evidence-referenced decisions.
- All tool calls use typed mappings; never accept free-form Shell or SQL.
- PostgreSQL is authoritative. Redis is not required for deterministic scoring.
- Default tests make no real DashScope, CLS, Milvus, Alertmanager, or Docker API call.
- First slice covers `APY-003` (upstream process down) and `APY-006` (upstream port mismatch), both in symptom family `nginx_upstream_5xx`.
- A scenario passes only when component and primary mechanism are correct, required evidence is grounded, and no hard gate is violated.
- Follow red-green-refactor and commit after every independently reviewable task.

---

## File Structure

Create the following focused files:

```text
apps/backend/src/super_ai/evaluation/
├── __init__.py             public evaluation contracts
├── domain.py               immutable scenario/run/result dataclasses
├── scenarios.py            public/oracle YAML loaders and validation
├── snapshot.py             frozen MCP-compatible observation runtime
├── artifacts.py            diagnostic records -> scoreable RunArtifact
├── scoring.py              deterministic score and hard gates
├── persistence.py          evaluation run/result repository service
└── runner.py               one-scenario orchestration

apps/backend/src/super_ai/aiops/
└── reasoning.py            validated hypotheses, updates, and final decision

apps/backend/scripts/
└── run_snapshot_benchmark.py

apps/backend/tests/
├── test_evaluation_scenarios.py
├── test_snapshot_evaluation_tools.py
├── test_aiops_reasoning_trace.py
├── test_evaluation_scoring.py
├── test_evaluation_persistence.py
└── test_snapshot_benchmark_runner.py

benchmarks/agentpy/scenarios/
├── APY-003/
│   ├── scenario.yaml
│   ├── ground_truth.yaml
│   ├── snapshot/tool_responses.yaml
│   └── provenance.yaml
└── APY-006/
    ├── scenario.yaml
    ├── ground_truth.yaml
    ├── snapshot/tool_responses.yaml
    └── provenance.yaml
```

Modify:

- `apps/backend/src/super_ai/aiops/diagnostics.py`
- `apps/backend/src/super_ai/aiops/__init__.py`
- `apps/backend/src/super_ai/memory/models.py`
- `apps/backend/src/super_ai/memory/repositories.py`
- `apps/backend/src/super_ai/memory/sqlalchemy.py`
- `apps/backend/tests/test_aiops_diagnostics.py`
- `apps/backend/tests/test_postgresql_migrations.py`
- `apps/backend/README.md`

---

### Task 1: Define scenario contracts and answer isolation

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/__init__.py`
- Create: `apps/backend/src/super_ai/evaluation/domain.py`
- Create: `apps/backend/src/super_ai/evaluation/scenarios.py`
- Test: `apps/backend/tests/test_evaluation_scenarios.py`

**Interfaces:**
- Produces: `load_public_scenario(path: Path) -> PublicScenario`
- Produces: `load_scenario_oracle(path: Path) -> ScenarioOracle`
- Produces: `ScenarioBundle(public: PublicScenario, oracle: ScenarioOracle, root: Path)`
- Produces: `validate_scenario_bundle(bundle: ScenarioBundle) -> None`

- [ ] **Step 1: Write failing loader and isolation tests**

```python
from pathlib import Path

import pytest

from super_ai.evaluation import load_public_scenario, load_scenario_oracle


def test_public_scenario_excludes_ground_truth(valid_scenario_dir: Path) -> None:
    scenario = load_public_scenario(valid_scenario_dir)
    serialized = repr(scenario)
    assert scenario.id == "APY-003"
    assert scenario.symptom_family == "nginx_upstream_5xx"
    assert "process_unavailable" not in serialized
    assert "benchmark_container_stopped" not in serialized


def test_oracle_requires_primary_component_mechanism_and_trigger(
    valid_scenario_dir: Path,
) -> None:
    oracle = load_scenario_oracle(valid_scenario_dir)
    assert oracle.primary_cause.component == "checkout-service"
    assert oracle.primary_cause.mechanism == "process_unavailable"
    assert oracle.primary_cause.trigger == "benchmark_container_stopped"


def test_loader_rejects_public_file_with_answer_keys(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "bad"
    scenario_dir.mkdir()
    (scenario_dir / "scenario.yaml").write_text(
        "id: BAD\nsymptom_family: x\nground_truth: leaked\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ground-truth keys"):
        load_public_scenario(scenario_dir)
```

Define `valid_scenario_dir` in this test module with `tmp_path`; it writes the
smallest valid `scenario.yaml` and `ground_truth.yaml`. The repository-owned
APY-003/APY-006 directories are intentionally introduced only in Task 2 so Task
1 can pass independently.

- [ ] **Step 2: Run the tests and verify import failure**

Run: `uv run pytest tests/test_evaluation_scenarios.py -q`

Expected: FAIL because `super_ai.evaluation` does not exist.

- [ ] **Step 3: Implement immutable contracts**

Define frozen, slotted dataclasses for:

```python
@dataclass(frozen=True, slots=True)
class PublicHypothesis:
    id: str
    description: str


@dataclass(frozen=True, slots=True)
class PublicScenario:
    id: str
    title: str
    symptom_family: str
    difficulty: str
    modes: tuple[str, ...]
    alert: dict[str, object]
    hypotheses: tuple[PublicHypothesis, ...]
    snapshot_file: str


@dataclass(frozen=True, slots=True)
class RootCause:
    component: str
    mechanism: str
    trigger: str


@dataclass(frozen=True, slots=True)
class EvidenceMilestone:
    id: str
    alternatives: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class ScenarioOracle:
    primary_cause: RootCause
    contributing_causes: tuple[RootCause, ...]
    causal_chain: tuple[str, ...]
    required_evidence: tuple[EvidenceMilestone, ...]
    required_rule_outs: tuple[str, ...]
    forbidden_claims: tuple[str, ...]
```

`load_public_scenario` reads only `scenario.yaml`, rejects keys named `ground_truth`, `oracle`, `primary_cause`, `required_evidence`, or `answer`, and never opens `ground_truth.yaml`. `load_scenario_oracle` is evaluator-only and reads only `ground_truth.yaml`.

- [ ] **Step 4: Run focused and strict checks**

Run: `uv run pytest tests/test_evaluation_scenarios.py -q`

Expected: PASS.

Run: `uv run ruff check src/super_ai/evaluation tests/test_evaluation_scenarios.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/evaluation apps/backend/tests/test_evaluation_scenarios.py
git commit -m "feat: define isolated benchmark scenario contracts"
```

---

### Task 2: Add the paired 502 Snapshot scenarios

**Files:**
- Create: `benchmarks/agentpy/scenarios/APY-003/scenario.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-003/ground_truth.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-003/snapshot/tool_responses.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-003/provenance.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-006/scenario.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-006/ground_truth.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-006/snapshot/tool_responses.yaml`
- Create: `benchmarks/agentpy/scenarios/APY-006/provenance.yaml`
- Modify: `apps/backend/tests/test_evaluation_scenarios.py`

**Interfaces:**
- Consumes: Task 1 scenario loaders.
- Produces: two valid scenario bundles with the same alert family and different mechanisms.

- [ ] **Step 1: Add a failing paired-case discrimination test**

```python
def test_paired_502_cases_have_same_symptom_and_different_mechanisms() -> None:
    roots = FIXTURE.parents[0]
    process_down = load_public_scenario(roots / "APY-003")
    port_mismatch = load_public_scenario(roots / "APY-006")
    process_oracle = load_scenario_oracle(roots / "APY-003")
    port_oracle = load_scenario_oracle(roots / "APY-006")

    assert process_down.symptom_family == port_mismatch.symptom_family
    assert process_down.alert["alertname"] == port_mismatch.alert["alertname"]
    assert process_oracle.primary_cause.mechanism == "process_unavailable"
    assert port_oracle.primary_cause.mechanism == "upstream_port_mismatch"
```

- [ ] **Step 2: Run and verify missing APY-006 failure**

Run: `uv run pytest tests/test_evaluation_scenarios.py::test_paired_502_cases_have_same_symptom_and_different_mechanisms -q`

Expected: FAIL with missing scenario file.

- [ ] **Step 3: Add explicit public, oracle, snapshot, and provenance YAML**

Both public files use `alertname: CheckoutUpstream5xxHigh`, expose hypotheses `upstream_process_down`, `upstream_port_mismatch`, and `dns_resolution_failure`, and contain no answer labels. APY-003 frozen observations include an exited checkout container plus Nginx `connection refused`; APY-006 includes a healthy container listening on `8080` while Nginx targets `8081`. Each oracle requires two positive evidence milestones and one rule-out milestone.

The paired cases are AgentPy-original. The mandatory GitHub provenance search found
no OpenSRE scenario that directly models either Docker/Nginx mechanism, so do not
claim an OpenSRE derivation. `provenance.yaml` contains:

```yaml
type: agentpy-original
origin:
  - AgentPy Docker Compose service topology
  - Nginx upstream failure observations reproduced by project-owned fixtures
validation_references:
  - https://github.com/nginx/nginx
  - https://github.com/docker/compose
license: project-license
```

The later full-catalog plan remains responsible for six genuinely OpenSRE-derived
cases and must pin each one to an exact upstream commit and file path before copying.

- [ ] **Step 4: Run the complete scenario test file**

Run: `uv run pytest tests/test_evaluation_scenarios.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add benchmarks/agentpy/scenarios apps/backend/tests/test_evaluation_scenarios.py
git commit -m "test: add paired nginx 502 benchmark scenarios"
```

---

### Task 3: Implement the frozen Snapshot tool runtime

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/snapshot.py`
- Modify: `apps/backend/src/super_ai/evaluation/__init__.py`
- Test: `apps/backend/tests/test_snapshot_evaluation_tools.py`

**Interfaces:**
- Consumes: `McpToolDefinition`, `RuntimeMcpClient`, public scenario snapshot path.
- Produces: `SnapshotMcpClient.from_yaml(path: Path) -> SnapshotMcpClient`
- Produces: `call_tool(name: str, arguments: Mapping[str, object]) -> object`
- Produces: ordered `SnapshotToolObservation` audit records without oracle data.

- [ ] **Step 1: Write failing discovery, replay, and rejection tests**

```python
async def test_snapshot_client_replays_typed_observations() -> None:
    client = SnapshotMcpClient.from_yaml(APY_003 / "snapshot" / "tool_responses.yaml")
    names = {tool.name for tool in await client.discover_tools()}
    assert names == {"InspectContainer", "InspectNginx"}

    result = await client.call_tool("InspectContainer", {"service": "checkout-service"})
    assert result["status"] == "exited"
    assert result["exitCode"] == 137


async def test_snapshot_client_rejects_unknown_tool_and_arguments() -> None:
    client = SnapshotMcpClient.from_yaml(APY_003 / "snapshot" / "tool_responses.yaml")
    with pytest.raises(McpClientError, match="not available"):
        await client.call_tool("ReadGroundTruth", {})
    with pytest.raises(McpClientError, match="arguments"):
        await client.call_tool("InspectContainer", {"service": "other-service"})
```

- [ ] **Step 2: Run and verify import failure**

Run: `uv run pytest tests/test_snapshot_evaluation_tools.py -q`

Expected: FAIL because `SnapshotMcpClient` is undefined.

- [ ] **Step 3: Implement exact-match deterministic replay**

Load a tuple of entries shaped as:

```yaml
tools:
  - name: InspectContainer
    description: Inspect one benchmark container
    input_schema:
      type: object
      required: [service]
      properties:
        service: {type: string}
    calls:
      - arguments: {service: checkout-service}
        result: {service: checkout-service, status: exited, exitCode: 137, health: unhealthy}
```

Canonicalize argument mappings with sorted JSON and require an exact registered call. Return a defensive copy. `get_langchain_tools` may reuse the existing `_langchain_tool` behavior through public `McpToolDefinition` and a local `StructuredTool`; it must not read any file after construction.

- [ ] **Step 4: Run focused tests and type checks**

Run: `uv run pytest tests/test_snapshot_evaluation_tools.py -q`

Expected: PASS.

Run: `uv run pyright src/super_ai/evaluation/snapshot.py tests/test_snapshot_evaluation_tools.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/evaluation apps/backend/tests/test_snapshot_evaluation_tools.py
git commit -m "feat: add deterministic snapshot tool runtime"
```

---

### Task 4: Add structured hypotheses and evidence-referenced decisions to LangGraph

**Files:**
- Create: `apps/backend/src/super_ai/aiops/reasoning.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/aiops/__init__.py`
- Modify: `apps/backend/tests/test_aiops_diagnostics.py`
- Test: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Produces: `HypothesisState(id: str, status: Literal["open", "supported", "refuted"], confidence: float, evidence_ids: tuple[str, ...])`
- Produces: `ObservationDecision(purpose: str, supports: tuple[str, ...], refutes: tuple[str, ...], summary: str)`
- Produces: `RootCauseDecision(component: str, mechanism: str, trigger: str, causal_chain: tuple[str, ...], evidence_ids: tuple[str, ...], confidence: float)`
- Produces: validators `parse_plan`, `parse_observation_decision`, and `parse_root_cause_decision`.

- [ ] **Step 1: Write failing validator tests**

```python
def test_observation_decision_requires_known_hypotheses_and_evidence() -> None:
    with pytest.raises(ValueError, match="unknown hypothesis"):
        parse_observation_decision(
            '{"purpose":"check","supports":["invented"],"refutes":[],"summary":"x"}',
            known_hypotheses={"upstream_process_down"},
        )


def test_root_cause_decision_requires_evidence_ids() -> None:
    with pytest.raises(ValueError, match="evidence"):
        parse_root_cause_decision(
            '{"component":"checkout-service","mechanism":"process_unavailable",'
            '"trigger":"container_stopped","causalChain":[],"evidenceIds":[],"confidence":0.9}',
            available_evidence_ids={"ev-1"},
        )
```

- [ ] **Step 2: Write a failing workflow trace test**

Construct a fake LLM with four ordered JSON responses: multi-tool plan and hypotheses, first observation update, second observation update, final root-cause decision. Use `SnapshotMcpClient` and assert persisted steps have phases `planner`, `executor`, `evidence_evaluation`, `executor`, `evidence_evaluation`, `decision`, `report`; every decision evidence ID exists in `aiops_diagnostic_evidence`.

- [ ] **Step 3: Run tests and confirm current SearchLog-only behavior fails**

Run: `uv run pytest tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py -q`

Expected: FAIL because the current planner discards non-`SearchLog` tools and has no decision node.

- [ ] **Step 4: Implement bounded multi-tool planning**

Extend `AiopsDiagnosticState` with `hypotheses`, `observation_decisions`, and `root_cause_decision`. Accept at most six validated steps, only from discovered tools, with fields `id`, `tool`, `arguments`, `purpose`, and `testsHypotheses`. Keep the existing one-step `SearchLog` fallback only when model planning is unavailable.

Change the graph to:

```text
planner -> executor -> evidence_evaluator -> replanner
replanner -> executor | decision
decision -> report
```

After each tool observation is persisted and has an evidence ID, ask the model for a compact `ObservationDecision`; validate all hypothesis IDs and attach the observation evidence ID server-side. The model never supplies arbitrary evidence IDs for observations.

- [ ] **Step 5: Implement the structured final decision**

The decision prompt contains only alert, public hypotheses, structured observations, and evidence summaries. Validate component/mechanism/trigger strings, `confidence` in `[0, 1]`, and evidence IDs against persisted evidence. Store the decision in a `decision` step and checkpoint, and include it in the report payload as `rootCauseDecision`.

The report remains Markdown but derives root cause fields from the validated decision. If no valid grounded decision exists, set `rootCauseDecision` to `null` and state that evidence is insufficient.

- [ ] **Step 6: Run focused regression and strict checks**

Run: `uv run pytest tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py -q`

Expected: PASS, including existing no-SOP and tool-failure behavior.

Run: `uv run ruff check src/super_ai/aiops tests/test_aiops_reasoning_trace.py tests/test_aiops_diagnostics.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/src/super_ai/aiops apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_aiops_diagnostics.py
git commit -m "feat: persist evidence-grounded diagnosis decisions"
```

---

### Task 5: Implement deterministic scoring and hard gates

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Create: `apps/backend/src/super_ai/evaluation/scoring.py`
- Modify: `apps/backend/src/super_ai/evaluation/__init__.py`
- Test: `apps/backend/tests/test_evaluation_scoring.py`

**Interfaces:**
- Produces: `build_run_artifact(task, steps, evidence, tool_calls, reports) -> RunArtifact`
- Produces: `score_run(artifact: RunArtifact, oracle: ScenarioOracle) -> EvaluationResult`
- Produces: dimension fields `outcome`, `diagnosis`, `evidence`, `process`, `safety`, `efficiency` totaling 100.

- [ ] **Step 1: Write failing perfect, confused, fabricated, and leakage tests**

```python
def test_exact_grounded_decision_passes_paired_case() -> None:
    result = score_run(process_down_artifact(), process_down_oracle())
    assert result.diagnosis == 25
    assert result.evidence == 20
    assert result.hard_gate is None
    assert result.passed is True


def test_same_symptom_wrong_mechanism_fails() -> None:
    result = score_run(process_down_artifact(), port_mismatch_oracle())
    assert result.diagnosis < 25
    assert result.passed is False
    assert "primary_mechanism_wrong" in result.failures


def test_unknown_evidence_caps_total_at_59() -> None:
    result = score_run(artifact_with_unknown_evidence(), process_down_oracle())
    assert result.evidence == 0
    assert result.total <= 59
    assert result.hard_gate == "fabricated_evidence"


def test_ground_truth_access_marks_run_invalid() -> None:
    result = score_run(artifact_with_tool("ReadGroundTruth"), process_down_oracle())
    assert result.validity == "invalid"
```

- [ ] **Step 2: Run and verify scorer import failure**

Run: `uv run pytest tests/test_evaluation_scoring.py -q`

Expected: FAIL because scorer contracts do not exist.

- [ ] **Step 3: Implement transparent dimension scorers**

Use exact component/mechanism/trigger comparison, evidence milestone alternatives, hypothesis rule-out status, decision-to-evidence membership, tool audit counts, and explicit safety events. Do not score Markdown keywords. For this Snapshot slice, outcome awards correct disposition and a grounded decision; Live health scoring is reserved for the later Live plan.

Store every awarded or withheld point as a `ScoreReason(code, points, maximum, evidence_ids)` so the report explains the score mechanically.

- [ ] **Step 4: Run tests and verify score invariants**

Run: `uv run pytest tests/test_evaluation_scoring.py -q`

Expected: PASS.

Add a parametrized test asserting all dimension values are within bounds and sum to `total` before hard-gate caps.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/src/super_ai/evaluation apps/backend/tests/test_evaluation_scoring.py
git commit -m "feat: score grounded benchmark diagnosis runs"
```

---

### Task 6: Persist evaluation runs and results in PostgreSQL

**Files:**
- Create: `apps/backend/alembic/versions/202608100001_add_evaluation_runs.py`
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Modify: `apps/backend/src/super_ai/memory/repositories.py`
- Modify: `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Create: `apps/backend/src/super_ai/evaluation/persistence.py`
- Modify: `apps/backend/tests/test_postgresql_migrations.py`
- Test: `apps/backend/tests/test_evaluation_persistence.py`

**Interfaces:**
- Produces: `EvaluationRunRecord` keyed by `run_id`, scenario, mode, suite version, agent version, model configuration, status, timestamps, and diagnostic task ID.
- Produces: `EvaluationResultRecord` keyed by `result_id` and `run_id`, with dimensions, total, validity, pass flag, failures, and score reasons.
- Produces: `EvaluationRepository.create_run`, `complete_run`, `save_result`, and `get_run_with_result`.

- [ ] **Step 1: Write failing migration metadata tests**

Add `aiops_evaluation_runs` and `aiops_evaluation_results` JSONB fields to the existing parametrized JSONB test. Add a PostgreSQL integration test proving `run_id` is unique and a result cascades when its run is deleted.

- [ ] **Step 2: Run migration tests and verify missing tables**

Run: `uv run pytest tests/test_postgresql_migrations.py -q`

Expected: FAIL because the new revision and tables do not exist.

- [ ] **Step 3: Add migration, models, repository contracts, and SQLAlchemy implementation**

Use revision `202608100001` with `down_revision = "202607300001"`. Store structured configurations and scores as JSONB; create indexes on `(scenario_id, created_at)`, `(status, created_at)`, and `diagnostic_task_id`. Never store API keys in model configuration.

`create_run` is idempotent by `run_id`; a second call with different scenario or agent version raises `ValueError`. `save_result` is one-to-one and rejects a result for a non-completed run.

- [ ] **Step 4: Run persistence and migration tests**

Run: `uv run pytest tests/test_evaluation_persistence.py tests/test_postgresql_migrations.py -q`

Expected: PASS against the existing PostgreSQL test fixture.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/alembic apps/backend/src/super_ai/memory apps/backend/src/super_ai/evaluation/persistence.py apps/backend/tests/test_evaluation_persistence.py apps/backend/tests/test_postgresql_migrations.py
git commit -m "feat: persist benchmark runs and scorecards"
```

---

### Task 7: Add the Snapshot benchmark runner and CLI report

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/runner.py`
- Create: `apps/backend/scripts/run_snapshot_benchmark.py`
- Modify: `apps/backend/src/super_ai/evaluation/__init__.py`
- Test: `apps/backend/tests/test_snapshot_benchmark_runner.py`

**Interfaces:**
- Consumes: scenario loaders, `SnapshotMcpClient`, a `DiagnosticRunAdapter`, artifact builder, scorer, persistence service.
- Produces: `SnapshotBenchmarkRunner.run(scenario_id: str, agent_version: AgentVersion) -> EvaluationResult`
- Produces CLI options `--scenario`, `--suite-version`, `--runs`, and `--output`.

- [ ] **Step 1: Write a failing end-to-end harness test with a scripted adapter**

```python
async def test_runner_keeps_oracle_outside_agent_and_writes_scorecard(tmp_path: Path) -> None:
    adapter = RecordingScriptedDiagnosticAdapter(process_down_artifact())
    runner = snapshot_runner(tmp_path, adapter=adapter)

    result = await runner.run("APY-003", agent_version=test_agent_version())

    assert result.passed is True
    assert "ground_truth" not in json.dumps(adapter.received_context)
    assert adapter.received_tools == {"InspectContainer", "InspectNginx"}
```

- [ ] **Step 2: Run and verify runner import failure**

Run: `uv run pytest tests/test_snapshot_benchmark_runner.py -q`

Expected: FAIL because `SnapshotBenchmarkRunner` is undefined.

- [ ] **Step 3: Implement orchestration with explicit Agent adapter boundary**

Define:

```python
class DiagnosticRunAdapter(Protocol):
    async def run(
        self,
        *,
        run_id: str,
        scenario: PublicScenario,
        mcp_client: RuntimeMcpClient,
    ) -> RunArtifact: ...
```

The runner loads public scenario and Snapshot tools first, passes only those objects to the adapter, then loads the oracle in a separate evaluator step after the Agent returns. Persist start, completion, and score atomically through the evaluation persistence service.

- [ ] **Step 4: Implement deterministic CLI serialization**

The CLI prints and optionally writes UTF-8 JSON containing scenario, run ID, dimension scores, failures, hard gate, validity, pass flag, duration, and score reasons. It returns exit code `0` for a valid completed run, `1` for a valid failed case, and `2` for invalid/infrastructure failure.

Implement exactly one production CLI adapter, `--adapter application`, backed by
the existing `AiopsDiagnosticService`. The scripted adapter is a test-only helper
defined in `test_snapshot_benchmark_runner.py`; it is never selectable from the CLI.
Neither adapter receives the oracle path.

- [ ] **Step 5: Run runner tests and CLI help**

Run: `uv run pytest tests/test_snapshot_benchmark_runner.py -q`

Expected: PASS.

Run: `uv run python scripts/run_snapshot_benchmark.py --help`

Expected: exit 0 and list `--scenario`, `--suite-version`, `--runs`, `--output`, and `--adapter`.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/src/super_ai/evaluation apps/backend/scripts/run_snapshot_benchmark.py apps/backend/tests/test_snapshot_benchmark_runner.py
git commit -m "feat: run snapshot sre benchmark scenarios"
```

---

### Task 8: Document and verify the first vertical slice

**Files:**
- Modify: `apps/backend/README.md`
- Create: `docs/aiops/agentpy-domainbench.md`
- Modify: `apps/backend/tests/test_local_development_docs.py`

**Interfaces:**
- Consumes: all preceding tasks.
- Produces: exact offline and application commands, score interpretation, answer-isolation warning, and next-phase boundary.

- [ ] **Step 1: Write a failing documentation contract test**

Assert the documentation contains:

```python
required = [
    "APY-003",
    "APY-006",
    "uv run python scripts/run_snapshot_benchmark.py",
    "ground_truth.yaml",
    "deterministic_score",
    "Snapshot 不启动故障容器",
]
```

- [ ] **Step 2: Run and verify documentation failure**

Run: `uv run pytest tests/test_local_development_docs.py -q`

Expected: FAIL because DomainBench documentation is missing.

- [ ] **Step 3: Write operational documentation**

Document the two paired cases, how the same 502 symptom maps to different mechanisms, how to run a single scenario, why the Agent cannot read the oracle, score dimensions, exit codes, and how to inspect the associated diagnostic task/evidence/tool audits/checkpoints.

State explicitly that this phase does not yet implement L1/L2 recovery, six Live scenarios, optional Judge, or the remaining eight Snapshot cases; those follow separate implementation plans after this vertical slice passes review.

- [ ] **Step 4: Run focused and full verification**

Run: `uv run pytest tests/test_evaluation_scenarios.py tests/test_snapshot_evaluation_tools.py tests/test_aiops_reasoning_trace.py tests/test_evaluation_scoring.py tests/test_evaluation_persistence.py tests/test_snapshot_benchmark_runner.py tests/test_aiops_diagnostics.py tests/test_local_development_docs.py -q`

Expected: PASS.

Run: `uv run ruff check src tests scripts`

Expected: PASS.

Run: `uv run pyright`

Expected: PASS.

- [ ] **Step 5: Run the existing deterministic CI backend lane**

Run from repository root: `powershell -ExecutionPolicy Bypass -File scripts/ci/run-backend-checks.ps1`

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/README.md docs/aiops/agentpy-domainbench.md apps/backend/tests/test_local_development_docs.py
git commit -m "docs: explain agentpy snapshot benchmark workflow"
```

---

## Plan Self-Review

- Spec coverage in this plan: answer isolation, paired differential cases, Snapshot replay, structured evidence/hypotheses/decision, deterministic scoring, PostgreSQL persistence, local runner, and default-offline tests.
- Deliberately deferred into later independent plans: approved-case RAG workflow, full 10-case catalog, L0 tool implementations beyond the paired case, L1/L2 recovery, six Docker Live drivers, optional Judge, experiment comparison UI/API, nightly Actions.
- All later tasks consume exact contracts defined by earlier tasks; no task depends on an undefined runtime type.
- No new package, cloud service, native binary, or license change is introduced.
