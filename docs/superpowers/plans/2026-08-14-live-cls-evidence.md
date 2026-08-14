# Live Benchmark Real CLS Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit real-CLS evidence mode to `APY-LIVE-PG-LOCK-001` while preserving the current PostgreSQL-only mode, deterministic 100-point scoring, safety gates, and answer isolation.

**Architecture:** The Live runner prepares an immutable evidence context after fault injection and before diagnosis. Local mode returns a no-network context; CLS mode uploads run-scoped structured logs, polls the official CLS MCP `SearchLog` until they are indexed, then gives the production diagnostic workflow a composite MCP client containing validated CLS search plus existing PostgreSQL tools. Infrastructure readiness is classified separately from scoreable Agent failures.

**Tech Stack:** Python 3.10+, asyncio, FastAPI project configuration, Tencent CLS Python SDK 1.0.4, official `Tencent/cls-mcp-server` 1.0.4, LangChain MCP adapters, PostgreSQL, pytest, Ruff, Pyright.

## Global Constraints

- Default evidence source remains `local`; ordinary CI must not contact Tencent Cloud.
- CLS mode never falls back to local or synthetic log evidence.
- Do not add dependencies, custom Tencent signing code, `tccli`, or cloud resource creation.
- Never serialize or print SecretId, SecretKey, Authorization headers, or SDK client configuration.
- CLS records must match Topic, bounded time window, `run_id`, `scenario_id`, and `incident_id`.
- CLS supplies request/error/timeline evidence; PostgreSQL tools remain authoritative for lock facts.
- `INFRA_INVALID` is not an Agent score of zero; scoreable Agent omissions are `VALID_FAIL`.
- Keep ground truth inaccessible to Agent, prompt, RAG, tools, reports, and recovery logic.
- Implement with red-green-refactor and commit after every task.

---

### Task 1: Add Evidence Context to the Live Lifecycle

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/domain.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/runner.py`
- Modify: `apps/backend/tests/test_live_benchmark_runner.py`

**Interfaces:**
- Produces: `EvidenceSource`, `LiveClsScope`, `LiveEvidenceReadiness`, `LiveEvidenceContext`.
- Produces: `LiveInfrastructureError(category)` for failures that invalidate the environment rather than score the Agent.
- Produces: `LiveEvidencePreparer.prepare(identity, scenario, observation) -> LiveEvidenceContext`.
- Changes: `LiveDiagnosticAdapter.diagnose(run_id, scenario, observation, evidence_context)`.

- [ ] **Step 1: Write the failing lifecycle tests**

Add a recording preparer and assert the order includes `prepare` between `inject` and `diagnose`; assert a preparer exception becomes `LiveBenchmarkError("evidence_preparation_failed")` and cleanup still runs.

```python
class RecordingEvidencePreparer:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def prepare(self, **values: object) -> LiveEvidenceContext:
        self.events.append("prepare")
        if self.fail:
            raise RuntimeError("secret-readiness-error")
        return LiveEvidenceContext.local(incident_id="APY-LIVE-PG-LOCK-001-run-1")


@pytest.mark.asyncio
async def test_runner_prepares_evidence_before_diagnosis() -> None:
    driver = RecordingDriver()
    runner = LiveBenchmarkRunner(
        scenario_root=LIVE_ROOT,
        driver=driver,
        evidence_preparer=RecordingEvidencePreparer(driver.events),
        diagnostic=RecordingDiagnostic(driver.events),
        recovery=RecordingRecovery(driver.events),
        evaluator=RecordingEvaluator(driver.events),
    )
    await runner.run("APY-LIVE-PG-LOCK-001", run_id="run-1")
    assert driver.events == [
        "preflight", "baseline", "inject", "prepare", "diagnose",
        "recover", "verify", "evaluate", "cleanup",
    ]
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `cd apps/backend && uv run pytest tests/test_live_benchmark_runner.py -q`

Expected: FAIL because the evidence contracts and `evidence_preparer` argument do not exist.

- [ ] **Step 3: Add immutable contracts and the runner phase**

Add these contracts to `domain.py`:

```python
EvidenceSource = Literal["local", "cls"]


@dataclass(frozen=True, slots=True)
class LiveClsScope:
    region: str
    topic_id: str
    from_ms: int
    to_ms: int
    run_id: str
    scenario_id: str
    incident_id: str


@dataclass(frozen=True, slots=True)
class LiveEvidenceReadiness:
    expected_log_count: int
    indexed_log_count: int
    attempts: int
    uploaded_at_ms: int
    searchable_at_ms: int


@dataclass(frozen=True, slots=True)
class LiveEvidenceContext:
    source: EvidenceSource
    incident_id: str
    cls_scope: LiveClsScope | None = None
    readiness: LiveEvidenceReadiness | None = None

    @classmethod
    def local(cls, *, incident_id: str) -> LiveEvidenceContext:
        return cls(source="local", incident_id=incident_id)


class LiveInfrastructureError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__("Live evidence infrastructure failed at a classified boundary.")
        self.category = category
```

Add `LiveEvidencePreparer` to `runner.py`, require it in `LiveBenchmarkRunner.__init__`, invoke it after confirmed injection, and pass the returned context into `diagnose`. Wrap the phase with `_classified(..., "evidence_preparation_failed")`. Update `_classified` to preserve `LiveInfrastructureError.category`, while a generic preparer exception remains `evidence_preparation_failed` and cancellation is re-raised.

- [ ] **Step 4: Run tests and commit**

Run: `cd apps/backend && uv run pytest tests/test_live_benchmark_runner.py -q`

Expected: PASS, including cleanup after preparation failure.

Commit: `git commit -am "feat: add live evidence preparation phase"`

---

### Task 2: Build Run-Scoped CLS Logs, Upload, and Bounded Readiness Polling

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/cls_evidence.py`
- Create: `apps/backend/tests/test_live_cls_evidence.py`
- Modify: `apps/backend/scripts/generate_and_upload_cls_logs.py`

**Interfaces:**
- Consumes: Task 1 evidence contracts.
- Produces: `build_live_cls_records`, `LiveClsLogUploader`, `LiveClsEvidencePreparer`.
- Produces protocols: `ClsUploadBoundary.put(records)` and `ClsSearchBoundary.search(scope)`.

- [ ] **Step 1: Write failing tests for record identity and polling**

Test that every record contains all indexed identity fields, does not reveal the hidden mechanism, and that polling accepts only a full matching set.

```python
def test_live_cls_records_are_scoped_without_revealing_oracle() -> None:
    records = build_live_cls_records(
        run_id="run-1",
        scenario_id="APY-LIVE-PG-LOCK-001",
        incident_id="APY-LIVE-PG-LOCK-001-run-1",
        now=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )
    assert {record["run_id"] for record in records} == {"run-1"}
    assert all(record["scenario_id"] == "APY-LIVE-PG-LOCK-001" for record in records)
    assert all(record["incident_id"] == "APY-LIVE-PG-LOCK-001-run-1" for record in records)
    assert "row_lock_blocking" not in json.dumps(records)
```

Use fake upload/search boundaries to cover first-attempt success, delayed indexing, partial results, timeout, upload failure, cancellation, and foreign-run results.

- [ ] **Step 2: Run tests and confirm failure**

Run: `cd apps/backend && uv run pytest tests/test_live_cls_evidence.py -q`

Expected: FAIL because `cls_evidence.py` does not exist.

- [ ] **Step 3: Implement the focused CLS boundary**

Create three safe events: request accepted, database response timeout, and alert emitted. Use fields `run_id`, `scenario_id`, `incident_id`, `service`, `environment`, `event`, `level`, `trace_id`, `component`, `message`, and `timestamp`. The timeout message may state that the order update exceeded its bounded database-response timeout, but must not name a row lock or blocker.

Implement the preparer with injected clock and sleep:

```python
class LiveClsEvidencePreparer:
    def __init__(
        self,
        *,
        region: str,
        topic_id: str,
        uploader: ClsUploadBoundary,
        searcher: ClsSearchBoundary,
        timeout_seconds: float = 90.0,
        poll_interval_seconds: float = 2.0,
        monotonic: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._region = region
        self._topic_id = topic_id
        self._uploader = uploader
        self._searcher = searcher
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
```

Upload with the existing `LogClient.put_log_raw` contract through `asyncio.to_thread`. Poll until all expected records with exact identity are visible or the monotonic deadline expires. Never return foreign records. Raise classified `LiveInfrastructureError` categories `cls_upload_failed`, `cls_index_timeout`, `cls_mcp_unavailable`, and `cls_readiness_inconsistent`; error text must not contain the underlying credential-bearing exception. Refactor the existing script to reuse the SDK group-building/upload helper without changing its profiles.

- [ ] **Step 4: Run tests and commit**

Run: `cd apps/backend && uv run pytest tests/test_live_cls_evidence.py -q`

Expected: PASS without network access.

Commit: `git add apps/backend/src/super_ai/evaluation/live/cls_evidence.py apps/backend/scripts/generate_and_upload_cls_logs.py apps/backend/tests/test_live_cls_evidence.py && git commit -m "feat: prepare run-scoped CLS live evidence"`

---

### Task 3: Expose a Validated Composite MCP Toolset

**Files:**
- Create: `apps/backend/src/super_ai/evaluation/live/evidence_client.py`
- Create: `apps/backend/tests/test_live_evidence_client.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/diagnostics.py`

**Interfaces:**
- Consumes: `LocalMcpClient`, `LivePostgresEvidenceMcpClient`, `LiveEvidenceContext`.
- Produces: `LiveCompositeEvidenceMcpClient.discover_tools`, `call_tool`, `get_langchain_tools`.
- Produces: `parse_cls_search_records(output) -> tuple[dict[str, object], ...]`.

- [ ] **Step 1: Write failing client contract and isolation tests**

Cover unique discovery, routing of PostgreSQL tools, routing of `SearchLog`, rejection of an incorrect Region/Topic/time window, filtering of foreign identities, and a tagged valid result containing `benchmarkEvidenceId="cls-live-request-timeout"`.

```python
@pytest.mark.asyncio
async def test_composite_cls_search_rejects_cross_run_records() -> None:
    client = LiveCompositeEvidenceMcpClient(
        postgres_client=LivePostgresEvidenceMcpClient(OBSERVATION),
        cls_client=FakeClsClient(records=[matching_record(), foreign_record()]),
        context=CLS_CONTEXT,
    )
    result = await client.call_tool("SearchLog", valid_search_arguments(CLS_CONTEXT))
    serialized = json.dumps(result)
    assert '"run_id":"run-1"' in serialized
    assert "run-2" not in serialized
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `cd apps/backend && uv run pytest tests/test_live_evidence_client.py -q`

Expected: FAIL because the composite client is absent.

- [ ] **Step 3: Implement strict routing and filtering**

In local mode expose only the three existing PostgreSQL tools. In CLS mode require exactly one discovered `SearchLog`, merge it with the PostgreSQL definitions, reject duplicate names, validate exact Region/Topic and a time range contained by the prepared scope, then parse and filter returned `LogJson` records by all three identities. Return a bounded structure:

```python
{
    "benchmarkEvidenceId": "cls-live-request-timeout",
    "recordCount": len(records),
    "records": records[:10],
}
```

Update the existing SearchLog result summarizer to consume this bounded mapping as well as the official MCP list payload. Do not pass readiness results into this client. The Agent must invoke the real MCP tool independently.

- [ ] **Step 4: Run tests and commit**

Run: `cd apps/backend && uv run pytest tests/test_live_evidence_client.py tests/test_live_diagnostic_adapter.py -q`

Expected: PASS with no cloud access.

Commit: `git add apps/backend/src/super_ai/evaluation/live/evidence_client.py apps/backend/src/super_ai/evaluation/live/diagnostics.py apps/backend/tests/test_live_evidence_client.py apps/backend/tests/test_live_diagnostic_adapter.py && git commit -m "feat: validate composite live evidence tools"`

---

### Task 4: Persist Source-Aware Evidence and Enforce CLS Scoring

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/artifacts.py`
- Modify: `apps/backend/src/super_ai/evaluation/domain.py`
- Modify: `apps/backend/src/super_ai/evaluation/scenarios.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/scoring.py`
- Modify: `benchmarks/agentpy/live/APY-LIVE-PG-LOCK-001/ground_truth.yaml`
- Modify: `apps/backend/tests/test_live_evaluation_scoring.py`
- Modify: `apps/backend/tests/test_evaluation_artifacts.py`

**Interfaces:**
- Adds: `ArtifactEvidence.source: str`.
- Adds: `ArtifactToolCall.arguments: JsonDict`.
- Adds: `ScenarioOracle.cls_required_evidence: tuple[EvidenceMilestone, ...]` with an empty default.
- Changes: `score_live_run` accepts `evidence_source: EvidenceSource = "local"`.

- [ ] **Step 1: Write failing artifact and scoring tests**

Add a 100/100 CLS artifact containing grounded `cls-live-request-timeout`, `postgres-wait-event-lock`, and `postgres-blocking-pid-edge` evidence. Assert missing CLS, missing PostgreSQL evidence, wrong query scope, and uncited CLS evidence are `VALID_FAIL`-equivalent score failures. Assert `cross_run_evidence` sets a hard gate and total zero while preserving `raw_total`.

```python
def test_cls_mode_requires_both_cls_and_postgres_citations() -> None:
    result = score_live_run(
        passing_cls_artifact(),
        load_live_oracle(SCENARIO),
        observation=OBSERVATION,
        recovery=RECOVERY,
        verification=VERIFICATION,
        evidence_source="cls",
    )
    assert result.required_evidence == 20
    assert result.citation_audit == 10
    assert result.total == 100
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `cd apps/backend && uv run pytest tests/test_evaluation_artifacts.py tests/test_live_evaluation_scoring.py -q`

Expected: FAIL because artifacts discard source/arguments and scoring has no evidence-source contract.

- [ ] **Step 3: Persist audit fields and extend deterministic scoring**

Append defaults so existing positional fixtures stay compatible:

```python
@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    record_id: str
    claim_id: str
    grounded: bool
    source: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactToolCall:
    name: str
    status: str
    risk_tier: Literal["L0", "L1", "L2", "L3"]
    approved: bool = False
    verified: bool = False
    arguments: JsonDict = field(default_factory=dict)
```

Populate both fields from persisted records. Add evaluator-only `cls_required_evidence` parsing with an empty default so existing Snapshot and Live scenarios remain compatible. Put the `cls-live-request-timeout` milestone under that key in this Live oracle. Score `required_evidence + cls_required_evidence` only when `evidence_source == "cls"`; local scoring keeps the existing two milestones and baseline. In CLS mode, citation credit requires decision evidence IDs to include at least one grounded `SearchLog` record and one grounded PostgreSQL record. Add `cross_run_evidence` to `_hard_gate`.

After artifact construction, classify the audited CLS call before scoring: no call, invalid Agent-selected arguments, or missing citation remains a scoreable `VALID_FAIL`; an unavailable audit repository, persistence gap, valid-scope call with failed status, or correct query returning no matching record raises `LiveInfrastructureError` with `cls_audit_persistence_failed`, `cls_search_failed`, or `cls_readiness_inconsistent`.

- [ ] **Step 4: Run tests and commit**

Run: `cd apps/backend && uv run pytest tests/test_evaluation_artifacts.py tests/test_live_evaluation_scoring.py -q`

Expected: PASS; local remains 100/100 and the complete CLS fixture is 100/100.

Commit: `git add apps/backend/src/super_ai/evaluation/artifacts.py apps/backend/src/super_ai/evaluation/domain.py apps/backend/src/super_ai/evaluation/scenarios.py apps/backend/src/super_ai/evaluation/live/scoring.py benchmarks/agentpy/live/APY-LIVE-PG-LOCK-001/ground_truth.yaml apps/backend/tests/test_evaluation_artifacts.py apps/backend/tests/test_live_evaluation_scoring.py && git commit -m "feat: score audited CLS live evidence"`

---

### Task 5: Wire Configuration, CLI Status, and Safe Reports

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `config/project.template.json`
- Modify: `config/project.test.json`
- Modify: `config/user.project.template.json`
- Modify: `apps/backend/tests/test_live_benchmark_cli.py`

**Interfaces:**
- Adds CLI: `run --evidence-source {local,cls}`, default `local`.
- Adds exit codes: `0=VALID_PASS`, `1=VALID_FAIL`, `2=INFRA_INVALID`.
- Adds safe result fields: `evidenceSource`, `validity`, and bounded non-secret readiness counts.

- [ ] **Step 1: Write failing CLI and redaction tests**

Assert default local mode, explicit CLS mode, rejection of unknown sources, exit/status mapping, missing CLS configuration failing closed, and removal of credentials/raw logs from stored reports.

```python
def test_run_evidence_source_defaults_local_and_accepts_cls() -> None:
    parser = build_parser()
    base = [
        "run", "--scenario", "APY-LIVE-PG-LOCK-001", "--run-id", "run-1",
        "--owner-user-id", "eval-user", "--knowledge-base-id", "kb-30-cards",
    ]
    assert parser.parse_args(base).evidence_source == "local"
    assert parser.parse_args(base + ["--evidence-source", "cls"]).evidence_source == "cls"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `cd apps/backend && uv run pytest tests/test_live_benchmark_cli.py -q`

Expected: FAIL because the flag and validity fields are absent.

- [ ] **Step 3: Wire local and CLS factories without exposing secrets**

Load merged `clsLogUpload`, `clsMcpServer`, and `mcp` sections only for explicit CLS mode. Construct the existing SDK client and `LocalMcpClient` inside a factory; pass only region, Topic, time scope, and readiness counts into runtime/report contracts. Map classified preparation/readiness categories to `infra_invalid` with exit code 2. Map a scoreable non-passing result to `failed`/exit 1 and a passing result to `passed`/exit 0. Never catch cancellation.

Add non-secret polling defaults:

```json
"liveClsEvidence": {
  "pollIntervalSeconds": 2,
  "indexWaitSeconds": 90,
  "queryLimit": 20
}
```

- [ ] **Step 4: Run tests and commit**

Run: `cd apps/backend && uv run pytest tests/test_live_benchmark_cli.py tests/test_live_benchmark_runner.py -q`

Expected: PASS without Tencent credentials.

Commit: `git add apps/backend/src/super_ai/evaluation/live/cli.py config/project.template.json config/project.test.json config/user.project.template.json apps/backend/tests/test_live_benchmark_cli.py && git commit -m "feat: expose explicit CLS live mode"`

---

### Task 6: Complete Offline Regression and Real CLS Acceptance Documentation

**Files:**
- Create: `apps/backend/tests/test_live_cls_acceptance.py`
- Modify: `apps/backend/pyproject.toml`
- Modify: `docs/tutorials/real-log-and-alert.md`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Adds pytest marker: `live_cls: uploads and queries the configured real Tencent CLS topic`.
- Adds one explicit real acceptance command; ordinary `uv run pytest` excludes `live_cls`.

- [ ] **Step 1: Add the explicit real-boundary acceptance test**

Mark the test `live_cls` and make it load required real configuration, upload run-scoped records, poll through the official MCP server, and assert exact identity. Missing or invalid configuration must fail the explicit run rather than silently use fake data.

```python
pytestmark = pytest.mark.live_cls


@pytest.mark.asyncio
async def test_real_cls_upload_and_search_are_run_scoped() -> None:
    preparer = build_live_cls_preparer()
    context = await preparer.prepare(
        identity=validate_run_id("live-cls-contract-001"),
        scenario=load_live_scenario(LIVE_SCENARIO),
        observation=LiveFaultObservation(101, 102, True, True),
    )
    assert context.source == "cls"
    assert context.readiness is not None
    assert context.readiness.indexed_log_count == context.readiness.expected_log_count
```

- [ ] **Step 2: Keep cloud acceptance outside ordinary CI**

Change pytest defaults to:

```toml
addopts = "-q --basetemp=var/pytest -m 'not live_llm and not live_docker and not live_cls'"
markers = [
  "live_llm: calls the configured real DashScope models",
  "live_docker: mutates the isolated local Docker Live Eval environment",
  "live_cls: uploads and queries the configured real Tencent CLS topic",
]
```

- [ ] **Step 3: Document exact local and full-chain commands**

Document prerequisites (`cls-mcp-server` running, Docker PostgreSQL/Milvus available, ignored user config populated), then provide:

```powershell
cd apps/backend
uv run pytest -m live_cls tests/test_live_cls_acceptance.py -q
uv run python -m super_ai.evaluation.live.cli run --scenario APY-LIVE-PG-LOCK-001 --run-id live-cls-pg-lock-001 --owner-user-id eval-user --knowledge-base-id kb-30-cards --evidence-source cls
```

Explain the three outcomes and exit codes. State that the command consumes real CLS and LLM quota and that reports contain no credentials.

- [ ] **Step 4: Run proportional verification**

Run offline checks first:

```powershell
cd apps/backend
uv run ruff check .
uv run pyright
uv run pytest
```

Expected: all offline checks PASS; live markers are deselected by configuration.

Then run the existing Docker local regression:

```powershell
uv run pytest -m live_docker tests/test_live_postgres_docker.py -q
```

Expected: PASS and cleanup leaves no synthetic blocker.

Run real CLS only after confirming the local MCP server and ignored credentials are available:

```powershell
uv run pytest -m live_cls tests/test_live_cls_acceptance.py -q
```

Expected: PASS with uploaded and indexed counts equal. Never print the loaded credentials.

- [ ] **Step 5: Commit the acceptance layer**

Commit: `git add apps/backend/tests/test_live_cls_acceptance.py apps/backend/pyproject.toml docs/tutorials/real-log-and-alert.md docs/aiops/agentpy-domainbench.md && git commit -m "test: verify real CLS live evidence chain"`

---

## Final Review Gate

- [ ] Confirm `git diff main...HEAD` contains only the approved CLS Live scope.
- [ ] Confirm no credential-like values appear with `git grep -n -E 'Secret(Id|Key)|TENCENTCLOUD_SECRET' -- ':!config/*.template.json'`; only field names and documented environment-variable names are allowed.
- [ ] Confirm local `APY-LIVE-PG-LOCK-001` remains 100/100.
- [ ] Confirm the complete real CLS run reports `VALID_PASS`, while injected uploader/poller failures report `INFRA_INVALID` without Agent score zero.
- [ ] Confirm Agent omission, wrong query scope, missing citation, or missing PostgreSQL lock evidence reports `VALID_FAIL`.
- [ ] Confirm cleanup runs after every failure and cancellation path.
- [ ] Use `verification-before-completion`, then `requesting-code-review`, before publishing the implementation branch.
