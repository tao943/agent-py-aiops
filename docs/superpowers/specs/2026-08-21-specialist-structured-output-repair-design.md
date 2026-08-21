# Specialist Structured Output Repair Design

## Context

The real `APY-LIVE-ORDER-POOL-LEAK-001` 3x3 A/B campaign proved that the
single-agent path is healthy while all three multi-agent runs fail before any
diagnostic tool executes. Both Runtime and Log Specialists report
`model_call_failed/provider_4xx` from their Local Plan role.

The configured Qwen provider advertises `json_mode`, but `SpecialistExecutor`
currently relies on `invoke_bounded_structured_role()`'s default
`function_calling` method. Existing planner and validator call sites already
select a structured-output method from provider capabilities. The Specialist
path is the inconsistent call site.

Failed Live terminal envelopes also retain only cleanup status. The diagnostic
task and bounded multi-agent investigation summary remain available in the
diagnostic store, but are absent from the immutable evaluation record.

## Reuse assessment

- Reuse the existing `LlmProvider.structured_output_method` capability and the
  `_provider_structured_output_method()` normalization already used by the
  diagnostic workflow.
- Reuse LangChain's existing `with_structured_output(method=...)` integration
  through `invoke_bounded_structured_role()`.
- Reuse the existing bounded `InvestigationMetrics` projection used for
  successful Live terminal envelopes.
- GitHub references checked: `langchain-ai/langchain` (MIT) and
  `QwenLM/Qwen-Agent` (Apache-2.0). Both support explicit provider-compatible
  structured-output selection. No new library or dependency is required.

Decision: wrapped adoption of the project's existing provider routing; custom
code is limited to propagating the selected method and projecting already
validated diagnostics into failed terminal records.

## Design

### 1. Strict provider capability propagation

`SpecialistExecutor` receives a required normalized
`structured_output_method`. It forwards that value to both bounded model roles:

- Local Plan
- Evidence Analysis

Production wiring obtains the value from the same provider capability helper
used by the main diagnostic graph. Tests and direct construction must select a
method explicitly, preventing a silent return to the library default.

The repair does not retry a Provider 4xx with an arbitrary alternate method.
Such a retry could hide a configuration error, spend additional model quota,
or change output semantics. Existing bounded retry remains limited to schema
correction within the selected method.

### 2. Failed terminal diagnostic projection

When a Live run fails after a diagnostic artifact exists, the classified
`LiveBenchmarkError` carries a bounded failure context containing:

- `diagnosticTaskId`
- investigation strategy and Specialist role statuses
- role duration/model/tool/evidence counts
- source-group, duplicate, conflict, and missing-domain counts
- aggregation checksum
- terminal failure category

The CLI persists these fields into the failed terminal envelope using the same
public metric names as a completed evaluation. The public CLI response remains
minimal and does not expose prompts, model responses, evidence content,
credentials, ground truth, or chain-of-thought.

Historical A/B artifacts are immutable and will not be rewritten.

### 3. Failure and safety behavior

- A genuine Provider 4xx remains a classified Specialist failure.
- If both required Specialists fail, aggregation remains terminal and recovery
  remains denied.
- If failure metadata is unavailable, terminal persistence still succeeds with
  cleanup metrics only.
- Diagnostic projection must pass existing forbidden-key and serialization
  validation.

## Test strategy

1. Unit tests prove `json_mode` reaches Local Plan and Evidence Analysis.
2. A regression test proves production Specialist wiring selects the provider
   capability rather than the helper default.
3. Runner tests prove a post-diagnosis classified failure carries only bounded
   diagnostic context.
4. CLI tests prove failed terminal envelopes persist the diagnostic task ID and
   multi-agent metrics while public output stays redacted.
5. Run focused pytest, Ruff, and Pyright checks for touched modules.
6. Run one real Multi canary for `APY-LIVE-ORDER-POOL-LEAK-001`. If healthy,
   report score, recovery outcome, Specialist metrics, model calls, and timing.
   Do not overwrite earlier campaign runs.

## Acceptance criteria

- Real Specialist calls use configured `json_mode`; no implicit
  `function_calling` remains on this path.
- Runtime and Log Specialists execute their allowed tools in the canary unless
  a newly classified external failure occurs.
- A failed terminal envelope can be traced to its diagnostic task and contains
  bounded investigation metrics.
- Recovery safety gates and answer isolation are unchanged.
- No new dependency, external service, permission, or license obligation is
  introduced.
