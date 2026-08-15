# Snapshot Tool Calling Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all ten Snapshot scenarios bind environment-owned tool arguments deterministically while preserving legitimate model-selected variants and exact Snapshot semantics.

**Architecture:** Add a small generic MCP argument-contract module that normalizes model arguments to exact registered calls and produces constrained model-facing schemas. `SnapshotMcpClient` derives request-scoped contracts from its frozen registrations; `ApplicationDiagnosticAdapter` passes them into the existing AIOps service, where Planner, Replanner, duplicate detection, and Executor share the same normalizer. Snapshot MCP remains the final exact-match boundary.

**Tech Stack:** Python 3.10+, dataclasses, JSON Schema/jsonschema, existing LangGraph AIOps workflow, PyYAML Snapshot fixtures, pytest/pytest-asyncio, Ruff, strict Pyright; no new dependency.

## Global Constraints

- Do not change ground truth, scenario answers, score weights, pass thresholds, or evidence requirements.
- Do not let Snapshot execution accept arbitrary or nearest-match arguments.
- Do not inject root-cause labels, expected evidence, recovery answers, or other oracle data.
- Planner and Replanner must use the same request-scoped normalizer.
- Fixed-field-only differences must share a duplicate fingerprint; legitimate variants must remain distinct.
- Ordinary CI remains offline and requires no LLM, PostgreSQL service, CLS, or paid API.
- Preserve the existing six-step budget, sufficiency gate, decision validator, recovery policy, and answer isolation.
- Do not run paid acceptance until every offline gate in Task 4 passes.

## File Structure

- Create `apps/backend/src/super_ai/mcp/tool_arguments.py`: generic immutable contracts, constrained schema generation, exact normalization, and bounded validation errors.
- Create `apps/backend/tests/test_tool_argument_contracts.py`: focused unit tests for singleton, multi-variant, ambiguity, unknown fields, schema constraints, and fingerprints.
- Modify `apps/backend/src/super_ai/evaluation/snapshot.py`: retain defensive copies of registered arguments and expose derived contracts without exposing evidence/results.
- Modify `apps/backend/tests/test_snapshot_evaluation_tools.py`: fixture-wide schema/contract validation for all ten Snapshot scenarios.
- Modify `apps/backend/src/super_ai/evaluation/runner.py`: pass optional contracts from a structural provider into `AiopsDiagnosticService`.
- Modify `apps/backend/src/super_ai/aiops/diagnostics.py`: constrain discovered schemas, normalize Planner/Replanner steps, fingerprint effective arguments, and fail closed before MCP.
- Modify `apps/backend/tests/test_aiops_reasoning_trace.py`: Planner/Replanner and Executor regression tests, including the exact `APY-013` mismatch.
- Modify `apps/backend/tests/test_snapshot_benchmark_runner.py`: prove the production adapter forwards Snapshot contracts without changing non-Snapshot adapters.

---

### Task 1: Generic runtime-owned tool argument contracts

**Files:**
- Create: `apps/backend/src/super_ai/mcp/tool_arguments.py`
- Create: `apps/backend/tests/test_tool_argument_contracts.py`

**Interfaces:**
- Produces: `ToolArgumentContract(tool_name: str, registered_calls: tuple[Mapping[str, object], ...])`.
- Produces: `ToolArgumentContractError(code: Literal["unknown_field", "ambiguous_variant", "invalid_variant", "schema_mismatch"])`.
- Produces: `normalize_tool_arguments(tool_name: str, arguments: Mapping[str, object], contracts: Mapping[str, ToolArgumentContract]) -> dict[str, object]`.
- Produces: `constrain_tool_definitions(definitions: Sequence[McpToolDefinition], contracts: Mapping[str, ToolArgumentContract]) -> list[McpToolDefinition]`.
- Produces: `tool_step_fingerprint(tool_name: str, arguments: Mapping[str, object]) -> str`.

- [ ] **Step 1: Write failing singleton and multi-variant normalization tests**

```python
from super_ai.mcp.tool_arguments import (
    ToolArgumentContract,
    ToolArgumentContractError,
    normalize_tool_arguments,
)


def test_singleton_contract_replaces_runtime_owned_arguments() -> None:
    contracts = {
        "InspectPostgresErrors": ToolArgumentContract(
            tool_name="InspectPostgresErrors",
            registered_calls=(
                {"service": "order-service", "windowMinutes": 15},
            ),
        )
    }
    assert normalize_tool_arguments(
        "InspectPostgresErrors",
        {"service": "order-service", "windowMinutes": 30},
        contracts,
    ) == {"service": "order-service", "windowMinutes": 15}
```

Assert both omitted arguments and
`{"service": "order-service", "windowMinutes": 30}` normalize to the exact 15-minute mapping.
Add a multi-call contract for `InspectClientRetryPolicy` with fixed `client=checkout-client` and
`view` values `effective-policy` and `sampled-timeline`. Assert a valid view selects one exact call,
an omitted view raises code `ambiguous_variant`, and an unregistered view raises `invalid_variant`.
Assert an extra field raises `unknown_field`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run from `apps/backend`:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_tool_argument_contracts.py -q -p no:cacheprovider --basetemp=var/pytest-tool-contract-red
```

Expected: collection fails because `super_ai.mcp.tool_arguments` does not exist.

- [ ] **Step 3: Implement immutable exact-call normalization**

Create the module with defensive copies and deterministic canonicalization. The core selection rule
must be equivalent to:

```python
@dataclass(frozen=True, slots=True)
class ToolArgumentContract:
    tool_name: str
    registered_calls: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        if not self.tool_name.strip() or not self.registered_calls:
            raise ValueError("Tool argument contracts require a name and registered calls.")

    @property
    def fixed_arguments(self) -> dict[str, object]:
        first = dict(self.registered_calls[0])
        return {
            key: value
            for key, value in first.items()
            if all(dict(call).get(key) == value and key in call for call in self.registered_calls)
        }
```

`normalize_tool_arguments` must:

1. return a copy unchanged when the tool has no runtime contract;
2. reject keys absent from every registered call;
3. ignore/replace mismatches only for `fixed_arguments`;
4. compare supplied non-fixed fields against each exact call;
5. return a defensive copy only when one call remains;
6. raise `ambiguous_variant` when several calls remain and `invalid_variant` when none remain.

Make `ToolArgumentContractError` expose only `code`, `tool_name`, and a bounded public message. Do
not include arbitrary model arguments in the message.

- [ ] **Step 4: Add constrained-schema and canonical fingerprint tests**

Add tests that construct an `McpToolDefinition` and assert the constrained definition keeps its
name, description, and server name. Compose, rather than replace, the original schema:

```python
constrained_schema = {
    "allOf": [
        copy.deepcopy(original_schema),
        {
            "oneOf": [
                {
                    "type": "object",
                    "required": sorted(call),
                    "additionalProperties": False,
                    "properties": {
                        key: {"const": copy.deepcopy(value)}
                        for key, value in call.items()
                    },
                }
                for call in registered_calls
            ]
        },
    ]
}
```

Tests must prove the original type/range rules remain active, required keys cannot be omitted,
additional fields are rejected, and each registered call matches exactly one `oneOf` branch.
Validate normalized mappings with `validator_for`.

```python
def test_fingerprint_uses_effective_arguments() -> None:
    exact = {"service": "order-service", "windowMinutes": 15}
    assert tool_step_fingerprint("InspectPostgresErrors", exact) == tool_step_fingerprint(
        "InspectPostgresErrors", dict(reversed(list(exact.items())))
    )
```

Also assert the two valid `view` variants have different fingerprints.

- [ ] **Step 5: Run unit tests and static checks**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_tool_argument_contracts.py -q -p no:cacheprovider --basetemp=var/pytest-tool-contract-green
& .\.venv\Scripts\python.exe -m ruff check src/super_ai/mcp/tool_arguments.py tests/test_tool_argument_contracts.py
& .\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe src/super_ai/mcp/tool_arguments.py tests/test_tool_argument_contracts.py
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit Task 1**

```powershell
git add apps/backend/src/super_ai/mcp/tool_arguments.py apps/backend/tests/test_tool_argument_contracts.py
git commit -m "feat: normalize runtime-owned tool arguments"
```

---

### Task 2: Derive safe contracts from all Snapshot registrations

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/snapshot.py`
- Modify: `apps/backend/tests/test_snapshot_evaluation_tools.py`

**Interfaces:**
- Consumes: `ToolArgumentContract` from Task 1.
- Produces: `SnapshotMcpClient.tool_argument_contracts: Mapping[str, ToolArgumentContract]` as a read-only, defensive view.
- Preserves: `SnapshotMcpClient.call_tool(name, arguments)` exact matching and observation behavior.

- [ ] **Step 1: Write failing derivation and isolation tests**

Add a test that loads `APY-013` and asserts:

```python
contracts = snapshot.tool_argument_contracts
assert contracts["InspectPostgresErrors"].registered_calls == (
    {"service": "order-service", "windowMinutes": 15},
)
assert all("evidence" not in repr(contract).lower() for contract in contracts.values())
```

Mutate a returned nested mapping and assert a second property read is unchanged. Add an inventory
test that loops over every scenario directory containing `snapshot/tool_responses.yaml`, requires
exactly ten scenarios, and for every tool/call:

- validates the call against the discovered input schema;
- normalizes the call back to itself;
- confirms every contract contains at least one registered call.

Add explicit assertions for the two multi-call tools and their registered variants.
Create a temporary malformed Snapshot YAML whose registered call violates its own schema (for
example, `windowMinutes: "fifteen"` against `type: integer`) and assert `from_yaml` raises a bounded
`ValueError` naming the tool but not serializing the call payload.

- [ ] **Step 2: Run Snapshot tool tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_snapshot_evaluation_tools.py -q -p no:cacheprovider --basetemp=var/pytest-snapshot-contract-red
```

Expected: FAIL because `tool_argument_contracts` is absent.

- [ ] **Step 3: Retain registered arguments independently of results**

Change `SnapshotMcpClient.__init__` to accept a `registered_arguments` mapping grouped by tool name,
populated during `from_yaml` from each call's public arguments. Do not derive contracts by parsing
private `_calls` canonical keys, and do not put `evidence_id` or `result` into the contract.

During `from_yaml`, validate every registered argument mapping against that tool's advertised input
schema using `validator_for`, `check_schema`, and `validate`. Convert `SchemaError` or
`ValidationError` into a bounded fixture `ValueError`. This runtime load check is authoritative;
the ten-scenario test is regression coverage rather than the only validator.

Expose the property using copies:

```python
@property
def tool_argument_contracts(self) -> Mapping[str, ToolArgumentContract]:
    return MappingProxyType(
        {
            name: ToolArgumentContract(
                tool_name=name,
                registered_calls=tuple(copy.deepcopy(calls)),
            )
            for name, calls in self._registered_arguments.items()
        }
    )
```

Ensure constructor validation rejects a discovered tool with zero registered calls as a fixture
configuration error. Preserve `ReadGroundTruth` filtering and exact `_calls` lookup.

- [ ] **Step 4: Run fixture-wide tests and static checks**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_snapshot_evaluation_tools.py tests/test_answer_isolation.py -q -p no:cacheprovider --basetemp=var/pytest-snapshot-contract-green
& .\.venv\Scripts\python.exe -m ruff check src/super_ai/evaluation/snapshot.py tests/test_snapshot_evaluation_tools.py
& .\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe src/super_ai/evaluation/snapshot.py tests/test_snapshot_evaluation_tools.py
```

Expected: all ten scenarios pass, answer-isolation stays green, and static checks exit 0.

- [ ] **Step 5: Commit Task 2**

```powershell
git add apps/backend/src/super_ai/evaluation/snapshot.py apps/backend/tests/test_snapshot_evaluation_tools.py
git commit -m "feat: derive exact snapshot argument contracts"
```

---

### Task 3: Apply one contract path to Planner and Replanner

**Files:**
- Modify: `apps/backend/src/super_ai/evaluation/runner.py`
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_snapshot_benchmark_runner.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`

**Interfaces:**
- Consumes: `Mapping[str, ToolArgumentContract]` from Snapshot.
- Extends: `AiopsDiagnosticService.__init__(..., tool_argument_contracts: Mapping[str, ToolArgumentContract] | None = None)`.
- Produces: `_normalize_plan_steps(...) -> tuple[list[JsonDict], list[ToolArgumentContractError]]` shared by Planner and Replanner.
- Preserves: existing `trusted_tool_arguments` behavior for Live `SearchLog`.

- [ ] **Step 1: Write a failing adapter-forwarding test**

In `test_snapshot_benchmark_runner.py`, use a recording `AiopsDiagnosticService` factory or patch
the constructor and assert `ApplicationDiagnosticAdapter.run` forwards the Snapshot client's
contract names. Also pass a normal `RuntimeMcpClient` test double with no contract provider and
assert an empty mapping is used, preserving non-Snapshot compatibility.

Use structural capability detection rather than adding Snapshot-only members to
`RuntimeMcpClient`:

```python
@runtime_checkable
class ToolArgumentContractProvider(Protocol):
    @property
    def tool_argument_contracts(self) -> Mapping[str, ToolArgumentContract]: ...
```

Define this protocol in `mcp/tool_arguments.py` and use `isinstance(mcp_client,
ToolArgumentContractProvider)` in the application adapter.

- [ ] **Step 2: Write failing Planner/Replanner parity tests**

In `test_aiops_reasoning_trace.py`, script an initial model plan for `APY-013` containing:

```python
{"tool": "InspectPostgresErrors", "arguments": {"service": "order-service", "windowMinutes": 30}}
```

Assert the persisted planner step contains 15 minutes and Snapshot receives the 15-minute call.
Script a gap-targeted Replanner response for `InspectPostgresWaitGraph` using
`{"database": "order-service", "windowMinutes": 60}` and assert it becomes
`{"database": "agent_py", "windowMinutes": 15}` through the same normalizer.

Add `APY-016` coverage proving `effective-policy` and `sampled-timeline` stay separate, while an
unknown `view` is filtered before execution.

- [ ] **Step 3: Run focused tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_snapshot_benchmark_runner.py tests/test_aiops_reasoning_trace.py -q -p no:cacheprovider --basetemp=var/pytest-planner-contract-red
```

Expected: new assertions fail because contracts are not forwarded or normalized.

- [ ] **Step 4: Wire contracts and constrain discovered definitions**

Store a defensive copy of `tool_argument_contracts` in `AiopsDiagnosticService`. After discovery,
call `constrain_tool_definitions` before building prompt payloads and storing definitions in graph
state. Proposal-only tool filtering remains unchanged.

Implement one helper in `diagnostics.py` that first applies the existing whole-tool
`bind_trusted_tool_arguments` and then calls `normalize_tool_arguments` for runtime contracts. Use
that helper in both `_create_plan` and `_replanner`. Validate only normalized steps with
`plan_matches_tool_contracts`.

The helper must return bounded errors rather than raw arguments. Planner behavior is:

- normalize initial Planner steps individually, drop invalid and post-normalization duplicate
  steps, and retain the remaining valid steps in original order up to the four-step limit;
- drop invalid and post-normalization duplicate Replanner steps individually;
- retain the existing generic fallback when the initial model plan has no valid steps;
- pass every generic fallback step through the same trusted binding, runtime normalization, exact
  contract validation, and duplicate filtering before accepting it;
- never construct a nearest registered call for an ambiguous multi-call variant.

Calculate every Planner/Replanner duplicate comparison with `tool_step_fingerprint` after
normalization. Keep `_step_fingerprint` as a thin wrapper or replace its call sites consistently.

- [ ] **Step 5: Run Planner/Replanner and Live regression checks**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_snapshot_benchmark_runner.py tests/test_aiops_reasoning_trace.py tests/test_live_diagnostic_adapter.py -q -p no:cacheprovider --basetemp=var/pytest-planner-contract-green
& .\.venv\Scripts\python.exe -m ruff check src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/runner.py tests/test_aiops_reasoning_trace.py tests/test_snapshot_benchmark_runner.py
& .\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe src/super_ai/aiops/diagnostics.py src/super_ai/evaluation/runner.py tests/test_aiops_reasoning_trace.py tests/test_snapshot_benchmark_runner.py
```

Expected: all commands exit 0; trusted Live `SearchLog` binding remains unchanged.

- [ ] **Step 6: Commit Task 3**

```powershell
git add apps/backend/src/super_ai/mcp/tool_arguments.py apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/src/super_ai/evaluation/runner.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_snapshot_benchmark_runner.py
git commit -m "feat: bind snapshot contracts in diagnostic planning"
```

---

### Task 4: Add Executor defense, APY-013 regression, and staged acceptance gates

**Files:**
- Modify: `apps/backend/src/super_ai/aiops/diagnostics.py`
- Modify: `apps/backend/tests/test_aiops_reasoning_trace.py`
- Modify: `apps/backend/tests/test_snapshot_benchmark_runner.py`
- Modify: `docs/aiops/agentpy-domainbench.md`

**Interfaces:**
- Consumes: Task 1 normalizer and bounded error codes.
- Produces: Executor diagnostic step failure payload with `errorCategory="invalid_arguments"` and
  `contractCode=ToolArgumentContractError.code`, without an MCP audit/call.
- Preserves: successful audit arguments exactly equal the normalized arguments sent to MCP.

- [ ] **Step 1: Write failing Executor bypass tests**

Construct graph state or invoke the existing workflow checkpoint path with an externally supplied
invalid/ambiguous multi-call step. Assert:

- `SnapshotMcpClient.observations` remains empty;
- no tool audit is created;
- one executor diagnostic step is persisted with `errorCategory=invalid_arguments` and bounded
  `contractCode`;
- `executor_attempt_count` increments once and control can continue to Replanner/decision;
- the payload contains neither the raw invalid value nor a full registered-call list.

Add a success assertion that the audit arguments and Snapshot observation arguments are identical.
Add duplicate tests for both initial Planner and Replanner output. Two steps that differ only in a
fixed model-supplied field must collapse before Executor. If a duplicate legacy/checkpoint step
still reaches Executor, it advances `plan_index` without incrementing `executor_attempt_count` and
does not create an MCP audit. A distinct valid multi-call variant still consumes one attempt when
executed.

- [ ] **Step 2: Write the APY-013 six-failure regression test**

Use the real `APY-013/snapshot/tool_responses.yaml` with a scripted model that proposes the six
argument mismatches from the acceptance report. Provide observation/sufficiency/decision responses
that are supported by the returned evidence. Assert:

```python
assert all(observation.arguments["windowMinutes"] == 15 for observation in snapshot.observations)
assert len(snapshot.observations) > 0
assert not any(
    step.payload.get("errorCategory") == "invalid_arguments"
    for step in persisted_steps
)
assert completed.result_payload["rootCauseDecision"] is not None
```

Also assert there are no `Snapshot arguments are not registered` failures and no
`step_budget_exhausted` caused by fixed-field-only retries.

- [ ] **Step 3: Run tests and verify RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_aiops_reasoning_trace.py tests/test_snapshot_benchmark_runner.py -q -p no:cacheprovider --basetemp=var/pytest-executor-contract-red
```

Expected: Executor bypass handling or APY-013 regression assertions fail before the defense is
implemented.

- [ ] **Step 4: Normalize and validate immediately before audit creation**

At the start of `_executor`, normalize the selected step using the same request-scoped contracts,
then validate against the constrained discovered schema. Replace `current_plan_step`, fingerprint,
event arguments, audit arguments, and MCP arguments with this effective step.

On `ToolArgumentContractError`, persist a failed executor step with only:

```python
{
    "planStepId": str(step.get("id") or ""),
    "tool": tool_name,
    "errorCategory": "invalid_arguments",
    "contractCode": exc.code,
}
```

Do not call `_create_audit`, do not emit a tool-started event, and do not call MCP. Increment the
bounded attempt counter once and use the existing graph routing.

Update the existing `duplicate_step` defensive branch separately: persist the bounded duplicate
diagnostic and advance `plan_index`, but do not increment `executor_attempt_count`. Since the plan
index advances, this cannot loop; it prevents normalized duplicates from exhausting the six-call
budget without granting evidence or score.

- [ ] **Step 5: Document ownership and staged real acceptance**

Add a short Tool Calling section to `docs/aiops/agentpy-domainbench.md` explaining:

- Snapshot resource/time scope is runtime-owned;
- genuine variants remain Agent-selected;
- exact matching still occurs at Snapshot MCP;
- contract normalization is not ground-truth injection;
- paid acceptance order is APY-013, then APY-014, APY-015, APY-016.

- [ ] **Step 6: Run the complete offline gate**

From `apps/backend`:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_tool_argument_contracts.py tests/test_snapshot_evaluation_tools.py tests/test_snapshot_benchmark_runner.py tests/test_aiops_reasoning_trace.py tests/test_live_diagnostic_adapter.py tests/test_answer_isolation.py tests/test_evaluation_safety.py -q -p no:cacheprovider --basetemp=var/pytest-tool-calling-final
& .\.venv\Scripts\python.exe -m ruff check src/super_ai/mcp/tool_arguments.py src/super_ai/evaluation/snapshot.py src/super_ai/evaluation/runner.py src/super_ai/aiops/diagnostics.py tests/test_tool_argument_contracts.py tests/test_snapshot_evaluation_tools.py tests/test_snapshot_benchmark_runner.py tests/test_aiops_reasoning_trace.py
& .\.venv\Scripts\python.exe -m pyright --pythonpath .\.venv\Scripts\python.exe src/super_ai/mcp/tool_arguments.py src/super_ai/evaluation/snapshot.py src/super_ai/evaluation/runner.py src/super_ai/aiops/diagnostics.py tests/test_tool_argument_contracts.py tests/test_snapshot_evaluation_tools.py tests/test_snapshot_benchmark_runner.py tests/test_aiops_reasoning_trace.py
```

Expected: all commands exit 0. If any command fails, stop before real-model execution.

- [ ] **Step 7: Commit Task 4**

```powershell
git add apps/backend/src/super_ai/aiops/diagnostics.py apps/backend/tests/test_aiops_reasoning_trace.py apps/backend/tests/test_snapshot_benchmark_runner.py docs/aiops/agentpy-domainbench.md
git commit -m "test: harden snapshot tool calling contracts"
```

- [ ] **Step 8: Run the paid APY-013 acceptance gate once**

Use the already configured project file, existing owner, and indexed 30-card knowledge base. Do not
print configuration contents or credentials:

```powershell
$EvalOwner = $env:AGENTPY_EVAL_OWNER_USER_ID
$EvalKnowledgeBase = $env:AGENTPY_EVAL_KNOWLEDGE_BASE_ID
if ([string]::IsNullOrWhiteSpace($EvalOwner) -or [string]::IsNullOrWhiteSpace($EvalKnowledgeBase)) {
    throw 'Set the existing test owner and indexed 30-card knowledge-base IDs in the AGENTPY_EVAL_OWNER_USER_ID and AGENTPY_EVAL_KNOWLEDGE_BASE_ID environment variables.'
}
& .\.venv\Scripts\python.exe scripts/run_snapshot_benchmark.py --scenario APY-013 --suite-version evidence-v2 --runs 1 --adapter application --config config/user.project.json --rag-mode on --owner-user-id $EvalOwner --knowledge-base-id $EvalKnowledgeBase --output var/benchmarks/APY-013-tool-contract-real.json
```

Expected: existing score threshold yields `VALID_PASS`, there are successful Snapshot observations,
and the report contains neither `missing_root_cause_decision` nor argument-contract failures. If it
fails semantically after successful calls, stop and diagnose the report without changing scoring or
running later scenarios.

- [ ] **Step 9: Sample APY-014 through APY-016 only after APY-013 passes**

Run the same command sequentially with the scenario and output name changed to `APY-014`, then
`APY-015`, then `APY-016`. Stop on the first infrastructure-invalid result or repeatable contract
defect. Record scores, duration, executed effective arguments, and failure reasons in the benchmark
documentation; never copy raw secrets or private model text.

## Plan Self-Review

- Spec coverage: exact binding, multi-call variants, schema constraints, shared Planner/Replanner
  normalization, post-normalization fingerprints, Executor defense, audit accuracy, ten-scenario
  coverage, answer isolation, and staged paid acceptance are each assigned to a task.
- Scope: no scenario, RAG, scoring, LangGraph topology, or recovery-policy changes are included.
- Type consistency: all tasks use the same `ToolArgumentContract`,
  `ToolArgumentContractProvider`, `normalize_tool_arguments`, and bounded error-code interfaces.
- Reuse: the plan extends existing trusted binding, JSON Schema validation, Snapshot exact lookup,
  and audit mechanisms; GitHub references remain reference-only and no dependency is added.
