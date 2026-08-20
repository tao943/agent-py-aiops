# Live Injection Failure Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist bounded per-check diagnostics when a Docker Live fault observation is not confirmed, while keeping CLI output and durable artifacts answer-isolated.

**Architecture:** Extend the existing `LiveBenchmarkError` boundary with a typed immutable diagnostic projection built only from `LiveFaultObservation`. Reuse the current CLI, `EvaluationRunEnvelope`, archive-first recorder, and PostgreSQL JSONB path; add optional allowlisted Live result fields without changing the artifact version or database schema.

**Tech Stack:** Python 3.10+, dataclasses, pytest/pytest-asyncio, existing Evaluation Archive and SQLAlchemy PostgreSQL repository, Ruff, Pyright.

## Global Constraints

- Do not add dependencies, external services, native binaries, or database migrations.
- Do not modify fault confirmation, recovery authorization, scoring, Agent Workflow, RAG, or CLS behavior.
- Never persist raw exceptions, events, logs, prompts, model responses, credentials, Oracle, Ground Truth, Primary Cause, or chain-of-thought.
- Persist only ordered check name/pass/source triples and explicitly declared scalar safe facts.
- Keep `artifactSchemaVersion="v1"`; all new Live result fields are optional and old artifacts remain readable.
- If a driver throws before returning an Observation, preserve the existing generic classified failure with no fabricated diagnostics.
- Invalid diagnostic data is omitted fail-closed while preserving `fault_injection_failed` and cleanup semantics.
- Do not run the full pytest suite and do not run a paid LLM/CLS canary in this change.

---

## File Structure

- Modify `apps/backend/src/super_ai/evaluation/live/runner.py`: own the typed error diagnostic projection and attach it only for an unconfirmed Observation.
- Modify `apps/backend/src/super_ai/evaluation/live/cli.py`: serialize the typed diagnostic into the terminal envelope and expose only failed check names through safe CLI/report output.
- Modify `apps/backend/src/super_ai/evaluation/history.py`: allow the three optional Live `resultPayload` fields while retaining recursive forbidden-key validation.
- Modify `apps/backend/tests/test_live_benchmark_runner.py`: verify typed capture, cleanup preservation, direct-inject failure behavior, and invalid diagnostic omission.
- Modify `apps/backend/tests/test_live_benchmark_cli.py`: verify Archive/repository payloads and bounded public output.
- Modify `apps/backend/tests/test_evaluation_history.py`: verify v1 round-trip, old-artifact compatibility, and recursive answer isolation.
- Modify `docs/aiops/agentpy-domainbench.md`: record the verified observability capability without claiming the real canary root cause is known.

### Task 1: Capture Typed Failure Diagnostics at the Runner Boundary

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/runner.py`
- Test: `apps/backend/tests/test_live_benchmark_runner.py`

**Interfaces:**
- Consumes: `LiveFaultObservation.checks` and `LiveFaultObservation.safe_facts`.
- Produces: `LiveFailureDiagnostics.from_observation(observation) -> LiveFailureDiagnostics | None`, `LiveFailureDiagnostics.failed_checks -> tuple[str, ...]`, and `LiveBenchmarkError.diagnostics: LiveFailureDiagnostics | None` for Task 2.

- [ ] **Step 1: Write RED tests for confirmed-false observations**

Update `RecordingDriver` to accept an optional Observation, then assert the existing false-observation test receives ordered typed data and retains it after successful cleanup:

```python
driver.observation = LiveFaultObservation(
    scenario_id="APY-LIVE-PG-LOCK-001",
    checks=(
        LiveCheck("pool_at_capacity", True, "driver"),
        LiveCheck("business_probe_timed_out", False, "driver"),
    ),
    safe_facts=(("poolCapacity", 3), ("businessProbeTimedOut", False)),
)

assert captured.value.diagnostics is not None
assert captured.value.diagnostics.failed_checks == ("business_probe_timed_out",)
assert captured.value.diagnostics.checks == driver.observation.checks
assert captured.value.diagnostics.safe_facts == driver.observation.safe_facts
assert captured.value.cleanup_succeeded is True
```

- [ ] **Step 2: Write RED tests for absent and invalid diagnostics**

Keep the direct `fail_at="inject"` case and assert `captured.value.diagnostics is None`. Add an Observation whose fact name is `ground_truth` and assert it remains `fault_injection_failed`, cleanup succeeds, and diagnostics are omitted rather than persisted or changing the failure classification.

```python
assert captured.value.category == "fault_injection_failed"
assert captured.value.diagnostics is None
assert captured.value.cleanup_succeeded is True
```

- [ ] **Step 3: Run the focused Runner tests and confirm RED**

Run:

```powershell
cd apps/backend
uv run pytest tests/test_live_benchmark_runner.py -q -p no:cacheprovider
```

Expected: new assertions fail because `LiveBenchmarkError` has no `diagnostics` and no typed projection exists.

- [ ] **Step 4: Implement the bounded typed projection**

In `runner.py`, add an immutable type next to `LiveBenchmarkError`:

```python
@dataclass(frozen=True, slots=True)
class LiveFailureDiagnostics:
    checks: tuple[LiveCheck, ...]
    safe_facts: tuple[tuple[str, str | int | float | bool], ...]

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    @classmethod
    def from_observation(
        cls, observation: LiveFaultObservation
    ) -> LiveFailureDiagnostics | None:
        try:
            _validate_failure_diagnostics(observation.checks, observation.safe_facts)
        except ValueError:
            return None
        return cls(observation.checks, observation.safe_facts)
```

Validation must require 1-64 checks, 0-64 facts, bounded safe identifiers, bounded check sources, finite floats, strings no longer than 256 characters, and JSON scalar values only. Canonicalized identifiers equal to any existing forbidden Artifact token are rejected. Do not accept an arbitrary Mapping.

Extend `LiveBenchmarkError.__init__` with keyword-only
`diagnostics: LiveFailureDiagnostics | None = None` and assign it without including values in the exception message. When `observation.confirmed` is false, raise:

```python
raise LiveBenchmarkError(
    "fault_injection_failed",
    stage="inject",
    diagnostics=LiveFailureDiagnostics.from_observation(observation),
)
```

The existing cleanup `finally` mutates only `cleanup_succeeded`, so the same exception object retains diagnostics.

- [ ] **Step 5: Run Runner tests and confirm GREEN**

Run the Step 3 command.

Expected: all `test_live_benchmark_runner.py` tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add apps/backend/src/super_ai/evaluation/live/runner.py apps/backend/tests/test_live_benchmark_runner.py
git commit -m "feat: capture live injection check failures"
```

### Task 2: Persist and Safely Expose Diagnostics

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/live/cli.py`
- Modify: `apps/backend/src/super_ai/evaluation/history.py`
- Test: `apps/backend/tests/test_live_benchmark_cli.py`
- Test: `apps/backend/tests/test_evaluation_history.py`

**Interfaces:**
- Consumes: `LiveBenchmarkError.diagnostics` from Task 1.
- Produces: optional Live `resultPayload.checkResults`, `resultPayload.failedChecks`, and `resultPayload.safeFacts`; safe CLI/report `result.failedChecks` only.

- [ ] **Step 1: Write RED Artifact contract tests**

Create a Live terminal envelope containing:

```python
result_payload={
    "failures": ["fault_injection_failed"],
    "failureStage": "inject",
    "checkResults": [
        {"name": "pool_at_capacity", "passed": True, "source": "driver"},
        {
            "name": "business_probe_timed_out",
            "passed": False,
            "source": "driver",
        },
    ],
    "failedChecks": ["business_probe_timed_out"],
    "safeFacts": {"poolCapacity": 3, "businessProbeTimedOut": False},
}
```

Assert `EvaluationRunEnvelope.from_json(envelope.to_json()) == envelope`, the checksum is stable, and the artifact version remains `v1`. Retain an existing Live envelope without the new fields to prove backward compatibility. Add parametrized nested `oracle`, `ground_truth`, `primary_cause`, `token`, and non-allowlisted top-level field rejection tests.

- [ ] **Step 2: Write RED CLI persistence/output tests**

Construct `LiveFailureDiagnostics` and raise a `LiveBenchmarkError` from the fake `execute`. Assert the Archive terminal envelope contains all three diagnostic fields exactly, while the returned safe payload contains only:

```python
"failedChecks": ["business_probe_timed_out"]
```

and does not contain `checkResults` or `safeFacts`. Assert `_live_failure_payload`, `write_safe_report`, and `read_safe_report` preserve `failedChecks` but drop injected `rawLogs`, `oracle`, and credential fields.

- [ ] **Step 3: Run focused contract tests and confirm RED**

Run:

```powershell
cd apps/backend
uv run pytest tests/test_live_benchmark_cli.py tests/test_evaluation_history.py -q -p no:cacheprovider
```

Expected: new Artifact fields are rejected and CLI persistence assertions fail.

- [ ] **Step 4: Extend the existing allowlists and serializers**

In `history.py`, add `checkResults`, `failedChecks`, and `safeFacts` only to the Live `_RESULT_KEYS` set.

In `cli.py`, add only `failedChecks` to `_SAFE_RESULT_FIELDS`. Introduce typed helper functions:

```python
def _failure_diagnostic_payload(error: LiveBenchmarkError) -> dict[str, object]:
    diagnostics = error.diagnostics
    if diagnostics is None:
        return {}
    return {
        "checkResults": [
            {"name": check.name, "passed": check.passed, "source": check.source}
            for check in diagnostics.checks
        ],
        "failedChecks": list(diagnostics.failed_checks),
        "safeFacts": dict(diagnostics.safe_facts),
    }


def _public_failure_diagnostic_payload(error: LiveBenchmarkError) -> dict[str, object]:
    if error.diagnostics is None:
        return {}
    return {"failedChecks": list(error.diagnostics.failed_checks)}
```

Merge `_failure_diagnostic_payload(exc)` only into the terminal envelope `result_payload`, and merge `_public_failure_diagnostic_payload(error)` into `_live_failure_payload` before `safe_output`. Do not put full diagnostics in metrics, metadata, stdout, report, or exception text.

- [ ] **Step 5: Run focused contract tests and confirm GREEN**

Run the Step 3 command.

Expected: all CLI and history tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add apps/backend/src/super_ai/evaluation/live/cli.py apps/backend/src/super_ai/evaluation/history.py apps/backend/tests/test_live_benchmark_cli.py apps/backend/tests/test_evaluation_history.py
git commit -m "feat: persist safe live failure diagnostics"
```

### Task 3: Targeted Regression, Static Verification, and Documentation

**Files:**
- Modify: `docs/aiops/agentpy-domainbench.md`
- Verify: all files modified in Tasks 1 and 2

**Interfaces:**
- Consumes: the complete typed Runner-to-Archive diagnostic path.
- Produces: a verified, documented observability capability ready for a separately approved real canary.

- [ ] **Step 1: Run the complete targeted regression set**

```powershell
cd apps/backend
uv run pytest tests/test_live_benchmark_runner.py tests/test_live_benchmark_cli.py tests/test_evaluation_history.py tests/test_evaluation_archive.py tests/test_evaluation_recording.py tests/test_evaluation_persistence.py tests/test_live_order_pool_contracts.py -q -p no:cacheprovider
```

Expected: all selected tests pass; no LLM, CLS, or Docker marker is invoked.

- [ ] **Step 2: Run Ruff and Pyright on changed Python files**

```powershell
uv run ruff check src/super_ai/evaluation/live/runner.py src/super_ai/evaluation/live/cli.py src/super_ai/evaluation/history.py tests/test_live_benchmark_runner.py tests/test_live_benchmark_cli.py tests/test_evaluation_history.py
uv run pyright src/super_ai/evaluation/live/runner.py src/super_ai/evaluation/live/cli.py src/super_ai/evaluation/history.py tests/test_live_benchmark_runner.py tests/test_live_benchmark_cli.py tests/test_evaluation_history.py
```

Expected: Ruff reports `All checks passed!`; Pyright reports zero errors.

- [ ] **Step 3: Verify the deterministic local failure path**

Run the focused CLI persistence test by exact node ID and inspect only its safe assertions:

```powershell
uv run pytest tests/test_live_benchmark_cli.py::test_fault_injection_failure_persists_safe_check_diagnostics -q -p no:cacheprovider
```

Expected: PASS, proving an unconfirmed Observation records the failed check name and bounded diagnostic fields without a real LLM/CLS call.

- [ ] **Step 4: Update DomainBench with verified boundaries**

Append a concise record stating: typed ordered check diagnostics are persisted in Archive/PostgreSQL; CLI exposes only failed names; malicious/invalid fields are omitted or rejected; targeted tests, Ruff, and Pyright passed; no full pytest or paid canary ran; the concrete cause of the previous canary remains unknown until a newly approved run produces the new evidence.

- [ ] **Step 5: Check diffs and commit documentation**

```powershell
git diff --check
git status --short
git add docs/aiops/agentpy-domainbench.md
git commit -m "docs: record live failure diagnostics"
```

Expected: only intended files have changed and the worktree is clean after the commit.

## Self-Review

- Spec coverage: typed projection, optional v1 fields, Archive/PostgreSQL persistence, bounded CLI output, direct-exception fallback, invalid-data fail-closed behavior, backward compatibility, security isolation, and targeted verification each map to an explicit task.
- Type consistency: Task 1 produces `LiveFailureDiagnostics.checks`, `.safe_facts`, and `.failed_checks`; Task 2 consumes those exact names.
- Scope: no dependency, migration, scoring, Agent, RAG, CLS, recovery authorization, or fault-threshold change is included.
- Placeholder scan: the plan contains no deferred implementation markers; every code edit and verification step has an exact file, interface, command, and expected result.
