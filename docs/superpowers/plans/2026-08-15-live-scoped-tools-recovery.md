# Live Scoped Tools and Recovery Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents for this plan.

**Goal:** Bind trusted CLS scope into Live `SearchLog` calls, expose a side-effect-free Nginx proposal tool, and persist safe recovery failure diagnostics.

**Architecture:** Extend the existing AIOps planner with an optional generic trusted-tool-argument mapping, populated only by the Live adapter from `LiveClsScope`. Keep the execution-time Live validator as defense in depth. Add the proposal contract to the existing Nginx component MCP client and carry bounded stage/authorization metadata through `LiveBenchmarkError` into the CLI allowlist.

**Tech Stack:** Python 3.10+, asyncio, LangGraph, LangChain structured tools, MCP tool definitions, jsonschema, pytest-asyncio, Ruff, Pyright.

## Global Constraints

- No new dependency or external service.
- Ordinary CI remains offline and does not require CLS, Docker, or LLM credentials.
- The official CLS MCP `SearchLog` remains the execution boundary.
- Trusted scope fields cannot be overridden by a model or SOP plan.
- Existing fail-closed scope and returned-record validation remains unchanged.
- Nginx proposal calls perform no write, reload, restart, route switch, Docker action, or signal.
- Ground truth, raw logs, credentials, private runtime IDs, and private reasoning never enter reports.
- Do not change scenario labels, ground truth, scoring weights, or pass thresholds.
- Before running plan commands from `apps/backend`, set
  `$env:SHARED_BACKEND_PYTHON='D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\apps\backend\.venv\Scripts\python.exe'`.

---

### Task 1: Bind trusted SearchLog arguments before plan validation

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cls_evidence.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/diagnostics.py`
- Test: `apps/backend/tests/test_live_diagnostic_adapter.py`
- Test: `apps/backend/tests/test_live_cls_evidence.py`

**Interfaces:**
- Produces: `build_cls_search_arguments(scope: LiveClsScope, *, limit: int = 20) -> dict[str, object]`.
- Produces: `bind_trusted_tool_arguments(plan: Sequence[JsonDict], trusted: Mapping[str, Mapping[str, object]]) -> list[JsonDict]`.
- Extends: `AiopsDiagnosticService.__init__(..., trusted_tool_arguments: Mapping[str, Mapping[str, object]] | None = None)`.
- Consumes: `LiveEvidenceContext.cls_scope` in `ApplicationLiveDiagnosticAdapter`.

- [x] **Step 1: Write failing tests for one canonical CLS argument builder**

Add a test that constructs a `LiveClsScope` and asserts the builder returns exact Region, TopicId,
From, To, a Query containing the quoted run/scenario/incident terms, and bounded Limit 20. Add
invalid-limit assertions for 0 and 101.

```python
arguments = build_cls_search_arguments(SCOPE)
assert arguments == {
    "Region": SCOPE.region,
    "TopicId": SCOPE.topic_id,
    "From": SCOPE.from_ms,
    "To": SCOPE.to_ms,
    "Query": (
        f'run_id:"{SCOPE.run_id}" AND scenario_id:"{SCOPE.scenario_id}" '
        f'AND incident_id:"{SCOPE.incident_id}"'
    ),
    "Limit": 20,
}
```

- [x] **Step 2: Run the builder tests and observe RED**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_live_cls_evidence.py -q
```

Expected: collection or import failure because `build_cls_search_arguments` does not exist.

- [x] **Step 3: Implement the canonical builder and reuse it in readiness polling**

Move the duplicated `SearchLog` mapping in `McpClsSearcher.search` behind the new function. Validate
`1 <= limit <= 100` in the builder and keep all existing parsing and polling behavior unchanged.

- [x] **Step 4: Run the builder tests and observe GREEN**

Run the Step 2 command. Expected: all `test_live_cls_evidence.py` tests pass.

- [x] **Step 5: Write failing tests for trusted plan binding**

Test the public pure helper with a model/SOP plan containing invalid `SearchLog` arguments. Assert
the effective plan uses the trusted mapping while preserving `id`, `tool`, `purpose`, and
`testsHypotheses`. Assert unrelated tools and the input plan are unchanged.

```python
bound = bind_trusted_tool_arguments(model_plan, {"SearchLog": trusted_arguments})
assert bound[0]["arguments"] == trusted_arguments
assert bound[0]["purpose"] == model_plan[0]["purpose"]
assert model_plan[0]["arguments"] == {"Query": "*"}
```

Add a service-level test in which the chat model returns an SOP/model `SearchLog` step with invalid
scope and assert `_create_plan` returns a contract-valid effective step rather than generic failure.

- [x] **Step 6: Run the trusted binding tests and observe RED**

Run:

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_live_diagnostic_adapter.py -q
```

Expected: import/signature/assertion failures because trusted binding is absent.

- [x] **Step 7: Implement immutable trusted binding in the AIOps planner**

Add the pure copy-on-write helper. Store a defensive copy of the optional trusted mapping in
`AiopsDiagnosticService`. Apply binding to both the generic fallback and parsed model plan before
`plan_matches_tool_contracts`. Preserve the existing wildcard generic step only when no trusted
mapping is supplied, so non-Live diagnostics retain current behavior.

- [x] **Step 8: Inject trusted SearchLog scope from the Live adapter**

When `evidence_context.cls_scope` exists, pass:

```python
trusted_tool_arguments={"SearchLog": build_cls_search_arguments(scope)}
```

When the source is local, pass no trusted mapping. Do not put root-cause labels or observation facts
in the trusted arguments.

- [x] **Step 9: Run Task 1 tests and commit**

Run:

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_live_cls_evidence.py tests/test_live_diagnostic_adapter.py tests/test_aiops_diagnostics.py -q
```

Expected: all selected tests pass.

Commit:

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/evaluation/live/cls_evidence.py apps/backend/src/super_ai/evaluation/live/diagnostics.py apps/backend/tests/test_live_cls_evidence.py apps/backend/tests/test_live_diagnostic_adapter.py
git commit -m "fix: bind trusted cls scope in live plans"
```

### Task 2: Expose the Nginx proposal-only MCP tool

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/nginx_timeout.py`
- Test: `apps/backend/tests/test_live_nginx_timeout_contracts.py`

**Interfaces:**
- Extends: `NginxTimeoutEvidenceMcpClient.discover_tools()` with `ProposeNginxTimeoutMitigation`.
- Extends: `NginxTimeoutEvidenceMcpClient.call_tool()` with schema-equivalent runtime validation and a bounded acknowledgement.
- Consumes: the existing `NginxProposalRecoveryService` audit contract without changing its policy.

- [x] **Step 1: Write failing discovery and execution tests**

Change the toolset assertion to include `ProposeNginxTimeoutMitigation`. Assert the definition
requires target, risk, rollback, verificationSteps, and humanApprovalRequired; forbids additional
properties; and constrains approval to `const: true`.

Call the tool with a complete proposal and assert the response contains only a stable proposal
evidence ID, `accepted: true`, and `humanApprovalRequired: true`. Assert no fixture file timestamp or
driver state changes.

- [x] **Step 2: Write failing rejection tests**

Parametrize missing/empty risk, rollback, fewer than two verification steps, false approval, wrong
target type, and extra fields. Each case must raise `McpClientError`; read-only tools must continue
to reject non-empty arguments.

- [x] **Step 3: Run Nginx contract tests and observe RED**

Run:

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_live_nginx_timeout_contracts.py -q
```

Expected: discovery and proposal execution assertions fail because the tool is absent.

- [x] **Step 4: Implement the side-effect-free proposal tool**

Add a dedicated JSON Schema and a private argument validator that uses the same semantic rules as
`NginxProposalRecoveryService`: non-empty text, at least two non-empty verification steps, exact
human approval, and no additional fields. Return a bounded acknowledgement without touching the
driver, filesystem, HTTP endpoints, Docker, or processes.

- [x] **Step 5: Run Nginx unit and Docker contract tests and commit**

Run offline unit tests first:

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_live_nginx_timeout_contracts.py tests/test_live_benchmark_cli.py -q
```

Expected: pass. Do not run the `live_docker` marker in this task.

Commit:

```powershell
git add apps/backend/src/super_ai/evaluation/live/nginx_timeout.py apps/backend/tests/test_live_nginx_timeout_contracts.py
git commit -m "feat: add nginx proposal only live tool"
```

### Task 3: Preserve safe failure stage and authorization metadata

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/runner.py`
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Test: `apps/backend/tests/test_live_benchmark_runner.py`
- Test: `apps/backend/tests/test_live_benchmark_cli.py`

**Interfaces:**
- Extends: `LiveBenchmarkError(category: str, *, stage: str | None = None, authorization_code: str | None = None)`.
- Extends safe result allowlist with `failureStage` and `authorizationCode`.

- [x] **Step 1: Write failing runner metadata tests**

Extend the recovery-oracle mismatch test to assert:

```python
assert captured.value.stage == "recover"
assert captured.value.authorization_code == "approval_required"
```

Add one diagnostic exception test and one cleanup failure test asserting `diagnose` and `cleanup`
stages respectively. Existing categories and exception chaining must remain unchanged.

- [x] **Step 2: Run runner tests and observe RED**

Run:

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_live_benchmark_runner.py -q
```

Expected: missing attributes or constructor keyword failures.

- [x] **Step 3: Implement bounded error metadata**

Add the two optional fields to `LiveBenchmarkError`. Pass explicit stages at runner boundaries and
attach `recovery.authorization_code` to `recovery_denied`. Cleanup errors must replace earlier stage
metadata only when cleanup itself fails, preserving the existing hard-failure behavior.

- [x] **Step 4: Write failing safe-report tests**

Extend `test_safe_json_output_drops_sensitive_and_oracle_fields` with valid `failureStage` and
`authorizationCode` plus disallowed raw error/model/tool fields. Add a CLI failure-path test that
raises a classified error and asserts the JSON result contains only the two safe metadata fields in
addition to the existing category, validity, and evidence source.

- [x] **Step 5: Run CLI tests and observe RED**

Run:

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_live_benchmark_cli.py -q
```

Expected: the new fields are missing from the safe output.

- [x] **Step 6: Add the fields to CLI serialization**

Allowlist `failureStage` and `authorizationCode`, then populate them from the caught
`LiveBenchmarkError`. Do not serialize `str(exc)`, causes, tool arguments, raw model output, or
resource identifiers.

- [x] **Step 7: Run Task 3 tests and commit**

Run:

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_live_benchmark_runner.py tests/test_live_benchmark_cli.py -q
```

Expected: pass.

Commit:

```powershell
git add apps/backend/src/super_ai/evaluation/live/runner.py apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/tests/test_live_benchmark_runner.py apps/backend/tests/test_live_benchmark_cli.py
git commit -m "feat: report live recovery failure stage"
```

### Task 4: Offline regression and optional real acceptance gate

**Files:**
- Modify only if verification exposes a scoped regression in files already listed above.
- Do not commit `apps/backend/var/**` reports or logs.

**Interfaces:**
- Consumes all Task 1-3 interfaces.
- Produces offline verification evidence and, only after offline success, a decision on paid Live reruns.

- [x] **Step 1: Run focused Live regression**

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_live_diagnostic_adapter.py tests/test_live_cls_evidence.py tests/test_live_evidence_client.py tests/test_live_nginx_timeout_contracts.py tests/test_live_benchmark_runner.py tests/test_live_benchmark_cli.py -q
```

Expected: pass with no skipped selected tests.

- [x] **Step 2: Run adjacent AIOps regression**

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest tests/test_aiops_diagnostics.py tests/test_evaluation_artifacts.py -q
```

Expected: pass.

- [x] **Step 3: Run Ruff and strict Pyright for modified files**

```powershell
& $env:SHARED_BACKEND_PYTHON -m ruff check src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/live/cls_evidence.py src/super_ai/evaluation/live/diagnostics.py src/super_ai/evaluation/live/nginx_timeout.py src/super_ai/evaluation/live/runner.py src/super_ai/evaluation/live/cli.py tests/test_live_diagnostic_adapter.py tests/test_live_cls_evidence.py tests/test_live_nginx_timeout_contracts.py tests/test_live_benchmark_runner.py tests/test_live_benchmark_cli.py
& $env:SHARED_BACKEND_PYTHON -m pyright --pythonpath $env:SHARED_BACKEND_PYTHON src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/live/cls_evidence.py src/super_ai/evaluation/live/diagnostics.py src/super_ai/evaluation/live/nginx_timeout.py src/super_ai/evaluation/live/runner.py src/super_ai/evaluation/live/cli.py tests/test_live_diagnostic_adapter.py tests/test_live_cls_evidence.py tests/test_live_nginx_timeout_contracts.py tests/test_live_benchmark_runner.py tests/test_live_benchmark_cli.py
```

Expected: both commands exit 0.

- [x] **Step 4: Run the ordinary offline backend suite**

```powershell
& $env:SHARED_BACKEND_PYTHON -m pytest -q
```

Expected: exit 0; configured live markers remain excluded by ordinary CI policy.

- [x] **Step 5: Inspect diff and commit any verification-only correction**

```powershell
git diff --check
git status --short
```

If no correction was needed, create no empty commit. If a focused correction was required, rerun the
failed gate and commit only the already-scoped files with `fix: harden scoped live recovery`.

- [x] **Step 6: Ask before consuming external model/CLS quota**

Do not automatically rerun the three paid LLM + CLS scenarios. Present offline evidence and request
explicit approval for sequential deadlock, Redis, and Nginx acceptance. If approved, use new run IDs,
run one scenario at a time, stop on exit 2, verify each report, and execute scoped cleanup after any
failed verification.
