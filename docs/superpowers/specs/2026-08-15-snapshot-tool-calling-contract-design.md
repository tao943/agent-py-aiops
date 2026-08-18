# Snapshot Tool Calling Contract Design

## 1. Goal

Repair the Tool Calling failures exposed by the real `APY-013` acceptance run without making the
Snapshot benchmark easier or changing its answers. The Agent must still choose relevant diagnostic
tools and build a supported root-cause decision, while infrastructure-owned call scope is bound and
validated deterministically.

The first real acceptance gate is `APY-013`. Only after it passes are `APY-014` through `APY-016`
sampled sequentially. Offline contract coverage must include all ten Snapshot scenarios before any
additional paid model run.

## 2. Problem Statement

The real model selected the correct PostgreSQL inspection tools but generated arguments outside the
registered Snapshot calls. For example, it requested a 30- or 60-minute window where the fixture
registered 15 minutes, and used `order-service` where the fixture expected database `agent_py`.
`SnapshotMcpClient` correctly rejected those calls. Six failed attempts exhausted the workflow
budget, leaving insufficient evidence and no supported root-cause decision.

This is a contract-ownership problem rather than a tool-selection problem. Snapshot identity,
resource scope, and captured time window are properties of the evaluation environment. Asking an
LLM to reproduce them exactly adds accidental uncertainty but no diagnostic value.

## 3. Constraints

- Do not change ground truth, scenario answers, score weights, pass thresholds, or evidence
  requirements.
- Do not let Snapshot execution accept arbitrary or nearest-match arguments.
- Do not inject root-cause labels, expected evidence, recovery answers, or other oracle data.
- Keep Agent-selected tool, purpose, and tested hypotheses auditable.
- Planner and Replanner must use the same binding and validation rules.
- Ordinary CI must remain offline and must not require an LLM, PostgreSQL service, CLS, or paid API.
- Add no dependency or external service.
- Preserve the existing six-step budget, sufficiency gate, decision validator, recovery policy, and
  answer-isolation boundaries.

## 4. Reuse Assessment

The project already provides the required foundations:

- request-scoped `trusted_tool_arguments` and `bind_trusted_tool_arguments`;
- discovered `McpToolDefinition` contracts and JSON Schema validation;
- shared Planner/Replanner plan validation;
- Snapshot's exact registered-call lookup;
- normalized plan fingerprints and persisted tool-call audit.

GitHub references inspected before design:

- `langchain-ai/langgraph` (MIT): `InjectedState` keeps runtime-owned inputs outside model
  generation;
- `langchain-ai/langchain` (MIT): `InjectedToolArg` hides injected arguments from the model-facing
  schema and reinjects them before execution;
- `modelcontextprotocol/python-sdk` (MIT): MCP inputs conform to the advertised JSON Schema,
  including full schema constraints.

These are reference-only. Adding another agent or tool runtime would duplicate the existing stack.
The selected design is wrapped adoption of the runtime-injected-argument pattern using current
project abstractions.

## 5. Considered Approaches

### 5.1 Schema constraints only

Derive `const` and `enum` constraints from registered Snapshot calls and expose them to the model.
This is small, but still spends model attention on values the runtime already knows and can fail if
the provider does not reliably honor nested schema constraints.

### 5.2 Runtime binding plus constrained schema — selected

Derive fields common to every registered call, bind them at runtime, and expose only genuine
multi-call choices as enums. Validate the effective call before execution. This preserves diagnostic
choice while eliminating accidental scope guessing and builds on current project mechanisms.

### 5.3 Fuzzy Snapshot matching — rejected

Map arbitrary model arguments to the nearest fixture call. This would make an unregistered query
appear to have returned real evidence, weaken auditability, and risk inflating benchmark scores.

## 6. Architecture

### 6.1 Derived Snapshot call contract

For each discovered Snapshot tool, derive a request-scoped contract from its registered calls:

- **fixed arguments**: fields with the same value in every registered call;
- **variant arguments**: primitive fields whose values differ across registered calls;
- **valid call tuples**: the exact complete argument mappings accepted by the fixture.

The derived contract exists only for the current scenario and is never persisted as Agent evidence.
Thirty-four of the current thirty-six tools have one registered call, so all their arguments are
fixed. The known multi-call tools remain explicit choices:

- `APY-015 / ProbeUpstreamHealth`: `service` is one of the two registered upstream targets;
- `APY-016 / InspectClientRetryPolicy`: `client` is fixed and `view` is one of the registered
  policy/timeline views.

If future registered calls differ in a way that cannot be represented safely as primitive enums,
the contract uses exact valid call tuples and rejects unsupported partial combinations. It does not
fall back to fuzzy matching.

### 6.2 Model-facing contract

The model continues to see the tool name, description, diagnostic purpose, and fields on which it
has a real choice. Fixed Snapshot fields are represented as schema `const` values or omitted from
the effective choice surface and injected later. Variant primitive fields are constrained with
`enum`.

Schema constraints improve planning guidance; they are not the security boundary. Runtime binding
and exact-call validation remain authoritative.

### 6.3 Shared plan normalization

One pure normalizer is applied after both Planner and Replanner output:

1. locate the derived contract for the selected tool;
2. inject or replace fixed arguments;
3. retain only a valid model-selected variant;
4. build the complete effective argument mapping;
5. validate it against the discovered JSON Schema;
6. validate it against an exact registered Snapshot call;
7. calculate the duplicate-call fingerprint from the effective arguments.

For a singleton registered call, the effective mapping is deterministic even if the model omits or
misstates fixed fields. For a multi-call tool, an omitted variant may be accepted only when the
remaining inputs identify exactly one registered call. Ambiguous or invalid variants are rejected.

### 6.4 Executor defense

The Executor performs a final normalize-and-validate check before calling MCP. This protects
against legacy checkpoints, forged plans, and future callers that bypass the planning boundary.

Expected Planner/Replanner validation failures never enter MCP and do not consume a tool-call audit
entry. If an invalid legacy or externally constructed step reaches the Executor, it records one
bounded `invalid_arguments` attempt containing safe contract diagnostics, performs no MCP call, and
returns control to the existing sufficiency/replanning flow.

### 6.5 Audit and duplicate detection

Persisted audit data records the exact effective arguments used for the MCP call, not the model's
discarded proposal. This makes the evidence request reproducible.

Duplicate fingerprints are calculated after binding. Calls that differ only in model-supplied
values for runtime-owned fields therefore count as the same call, preventing repeated retries from
consuming the six-step budget. Legitimate multi-call variants retain distinct fingerprints.

## 7. Data Flow

```text
Scenario Snapshot registrations
  -> derive fixed fields, allowed variants, and exact call tuples
  -> expose constrained tool contract to Planner or Replanner
  -> model selects tool, purpose, hypotheses, and genuine variant
  -> inject fixed runtime scope
  -> JSON Schema validation
  -> exact registered-call validation
  -> normalized duplicate fingerprint
  -> Snapshot MCP exact call
  -> evidence audit
  -> sufficiency and root-cause decision
  -> recovery policy and benchmark scoring
```

## 8. Error Handling

- Unknown tool names remain invalid plans.
- A fixed-field mismatch is replaced by the trusted registered value and is not treated as evidence
  of diagnostic failure.
- An unknown or ambiguous variant is rejected before MCP with a bounded validation reason.
- A derived contract with zero registered calls is an invalid Snapshot fixture/configuration error,
  not an Agent score failure.
- A registered call that violates its advertised JSON Schema is also a fixture/configuration error.
- Snapshot MCP retains exact matching and rejects any bypassed unregistered call.
- Runtime normalization never selects a root cause, evidence item, or recovery action.
- Existing budget exhaustion and safe recovery deferral behavior remain unchanged.

## 9. Testing and Acceptance

### 9.1 Offline contract tests

All ten Snapshot scenarios must be loaded and checked without an LLM:

1. every registered call satisfies its discovered JSON Schema;
2. every singleton tool binds to its exact registered arguments;
3. fixed fields override omitted and incorrect model values;
4. every multi-call variant resolves to the intended exact call;
5. unknown and ambiguous variants are rejected before MCP;
6. Planner and Replanner produce the same effective arguments for the same step;
7. fingerprints use effective arguments and collapse fixed-field-only differences;
8. distinct valid variants retain distinct fingerprints;
9. Executor rejects invalid bypass/legacy input without invoking MCP;
10. audit records effective arguments for successful calls;
11. the six-step budget and existing failure semantics remain intact;
12. answer-isolation tests continue to prove no ground-truth access.

Targeted regression tests must reproduce the real `APY-013` mismatches: 30/60-minute windows and
the incorrect PostgreSQL database name must normalize to the registered call rather than create six
failed MCP attempts.

### 9.2 Verification gates

Run targeted Tool Calling and Snapshot tests first, followed by offline Ruff, strict Pyright, and
the relevant benchmark/answer-isolation regression suite. No paid model run begins until these
gates pass.

### 9.3 Real acceptance

1. Run `APY-013` once with the configured real model and indexed 30-card RAG knowledge base.
2. Require the existing benchmark result to pass without changing its scoring inputs or threshold.
3. If `APY-013` fails, stop and diagnose that report; do not spend quota on later scenarios.
4. If it passes, run `APY-014`, `APY-015`, and `APY-016` sequentially, stopping on the first
   infrastructure-invalid result or repeatable contract defect.

The repair succeeds when offline coverage is green and `APY-013` reaches the existing pass
threshold through real evidence collection and a supported diagnosis. A remaining semantic
diagnosis failure after correct tool execution is reported separately and is not hidden by contract
fallbacks.

## 10. Non-goals

- Changing benchmark content, required evidence, root-cause aliases, recovery policy, or scoring.
- Adding more Snapshot scenarios or RAG cards.
- Changing the LangGraph topology, step budget, or general decision validator.
- Making Snapshot fixtures answer arbitrary time ranges or resource names.
- Adding provider-specific structured-output libraries.
- Re-running the complete paid benchmark suite before the staged gates pass.
