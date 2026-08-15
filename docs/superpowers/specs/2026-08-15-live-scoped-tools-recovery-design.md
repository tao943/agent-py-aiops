# Live Scoped Tools and Recovery Diagnostics Design

## 1. Goal

Repair the production LLM + CLS Live path exposed by the four-scenario acceptance run:

- prevent trusted Live identity and time scope from being delegated to model-generated
  `SearchLog` arguments;
- keep the existing fail-closed CLS validator and cross-run evidence isolation;
- expose a real proposal-only Nginx mitigation tool without adding a write capability;
- preserve a safe failure stage and recovery authorization code in the Live report.

The benchmark continues to evaluate whether the Agent selects relevant tools, cites current-run
evidence, identifies the correct root cause, rules out alternatives, and produces an authorized
recovery or proposal. It no longer evaluates whether an LLM can reproduce infrastructure-owned
CLS identifiers and millisecond boundaries.

## 2. Constraints

- No new dependency or external service.
- Ordinary CI remains offline and does not require CLS, Docker, or LLM credentials.
- `SearchLog` must still reach the configured official CLS MCP server.
- Region, Topic, time window, run ID, scenario ID, and incident ID come only from
  `LiveEvidenceContext.cls_scope`.
- The existing validator remains fail-closed; wildcard and cross-run queries remain invalid.
- The persisted tool audit records the exact effective `SearchLog` arguments sent to MCP.
- Nginx is proposal-only: no file write, reload, restart, route switch, or process signal.
- Ground truth and oracle data remain inaccessible to Agent, Prompt, RAG, and reports.
- Safe CLI reports must not contain credentials, database connection data, raw CLS logs, client
  identifiers, or private runtime handles.

## 3. Reuse Assessment

The implementation uses existing project-owned `LiveClsScope`, `LiveEvidenceContext`,
`McpToolDefinition`, composite Live MCP client, AIOps plan validation, tool-call audit, and
`LiveRecoveryRecord` boundaries.

GitHub references inspected before design:

- `modelcontextprotocol/python-sdk` (MIT): typed and hand-authored MCP input schemas with
  validation before handler execution;
- `openai/openai-agents-python` (MIT): tool input guardrails at the execution boundary;
- `langchain-ai/langgraph` (MIT): explicit human-interrupt contracts for actions requiring
  approval.

These are reference-only. Direct adoption would duplicate dependencies already present or add a
second agent runtime. The selected design is a small project-owned adapter around the existing
MCP and diagnostic workflow.

## 4. Architecture

### 4.1 Trusted SearchLog binding

The Live diagnostic adapter derives one immutable `SearchLog` argument mapping from
`LiveClsScope`:

```text
Region  = scope.region
TopicId = scope.topic_id
From    = scope.from_ms
To      = scope.to_ms
Query   = run_id AND scenario_id AND incident_id
Limit   = bounded project value
```

The mapping is passed to `AiopsDiagnosticService` through a generic trusted-tool-arguments
interface. The interface is not Live-specific: it maps a discovered tool name to arguments owned
by the execution environment.

During planning, a `SearchLog` step retains the Agent-selected tool, purpose, and tested
hypotheses, while its arguments are replaced by the trusted mapping before contract validation and
persistence. Generic fallback planning uses the same trusted mapping instead of `Query: "*"` and a
24-hour window. Other tools and non-Live diagnostics remain unchanged.

This is an execution envelope, not answer injection: it contains resource scope only and no
incident facts, root cause label, recovery action, or oracle value.

### 4.2 Defense in depth

`ScopedLiveEvidenceMcpClient.call_tool` continues to validate the effective arguments against
`LiveClsScope` immediately before forwarding to the official MCP client. It continues to filter
returned records by all three identity fields.

The planner binding prevents expected model formatting mistakes. The execution validator protects
against programming errors, forged plans, and future callers that bypass planning. Neither layer
accepts wildcard, out-of-window, cross-topic, or cross-run queries.

### 4.3 Nginx proposal-only tool

The Nginx component client exposes `ProposeNginxTimeoutMitigation` alongside its read-only evidence
tools. Its input schema requires:

- `target`: the affected component;
- `risk`: non-empty risk explanation;
- `rollback`: non-empty rollback instructions;
- `verificationSteps`: a non-empty list of executable verification steps;
- `humanApprovalRequired`: exactly `true`.

Calling the tool validates and returns a bounded acknowledgement only. It performs no Nginx,
filesystem, network-control, Docker, or process mutation. The completed call remains in the normal
tool-call audit, allowing `NginxProposalRecoveryService` to validate it against the independently
observed root cause and the no-write boundary.

The existing recovery service remains the final authorization policy. Adding the tool does not
make every proposal authorized: incorrect target, missing risk or rollback, unusable verification,
missing human approval, wrong root cause, or any write-like audited call still produces
`proposal_denied`.

### 4.4 Failure diagnostics

`LiveBenchmarkError` carries a safe stage and an optional safe authorization code in addition to
its existing category. The runner attaches:

- `failureStage=diagnose` for diagnostic workflow failures;
- `failureStage=recover` plus the recovery record's authorization code when the recovery contract
  is rejected;
- the corresponding bounded stage for injection, evidence preparation, verification, evaluation,
  and cleanup failures.

The CLI allowlist persists only `failureStage` and `authorizationCode`. It does not serialize raw
exceptions, model responses, tool arguments, CLS records, resource IDs, or credentials.

For `recovery_denied`, the report distinguishes examples such as `deadlock_decision_required`,
`redis_decision_required`, and `proposal_denied`. Upstream diagnostic failures retain their own
stage/category rather than being represented as an unexplained recovery rejection.

## 5. Data Flow

```text
Live fault injection
  -> CLS upload and bounded readiness polling
  -> immutable LiveClsScope
  -> Live diagnostic adapter
  -> trusted SearchLog argument binding
  -> AIOps planner chooses tools and purposes
  -> execution validator rechecks scope
  -> official CLS MCP SearchLog
  -> persisted bounded evidence and tool audit
  -> root-cause decision
  -> scenario recovery/proposal policy
  -> independent verification
  -> safe score or staged failure report
  -> scoped fixture cleanup
```

## 6. Error Handling

- Missing CLS scope in CLS mode is an infrastructure/configuration failure before Agent execution.
- A caller-provided SearchLog plan cannot override trusted scope fields.
- A post-binding scope validation failure is treated as an implementation/infrastructure defect,
  not an Agent query-syntax score.
- Official MCP unavailability, valid-scope search failure, or readiness inconsistency retains the
  existing `INFRA_INVALID` classification.
- Agent omission of SearchLog, failure to cite returned CLS evidence, incorrect root cause, unsafe
  recovery, or incomplete proposal remains `VALID_FAIL`.
- Cleanup always remains separate from Agent recovery credit and is scoped to the current run.

## 7. Testing

Offline tests must cover:

1. Generic Live fallback uses the trusted SearchLog mapping rather than wildcard scope.
2. A model/SOP SearchLog step has its arguments replaced while retaining purpose and hypotheses.
3. Non-SearchLog steps and non-Live diagnostics are unchanged.
4. The persisted audit receives the effective scoped arguments.
5. The execution validator still rejects wildcard, wrong topic, invalid time, and missing identity
   terms when called outside the trusted planning path.
6. The Nginx proposal tool is discoverable, schema-validating, side-effect-free, and auditable.
7. Complete Nginx proposal input authorizes proposal-only recovery without execution.
8. Missing approval, risk, rollback, verification, correct target, or correct diagnosis is denied.
9. Failure reports include only allowlisted `failureStage` and `authorizationCode` fields.
10. Existing Live runner, CLI routing, scoring, answer isolation, Ruff, and strict Pyright checks
    remain green.

Real acceptance is run sequentially after offline verification:

1. PostgreSQL deadlock;
2. Redis maxclients;
3. Nginx timeout.

Each run may produce `VALID_PASS` or a meaningful `VALID_FAIL`, but it must no longer fail because
the workflow generated an unscoped SearchLog call. Exit code `2` still stops the sequence.

## 8. Non-goals

- Changing scenario ground truth, canonical labels, score weights, or pass thresholds.
- Making PostgreSQL or Redis recovery less restrictive.
- Automatically approving or applying an Nginx change.
- Adding a general human-approval UI or LangGraph interrupt in this change.
- Persisting private chain-of-thought or raw model output.
- Re-running the full four-scenario paid acceptance before offline gates pass.
