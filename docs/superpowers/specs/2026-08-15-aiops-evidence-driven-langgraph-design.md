# Evidence-Driven AIOps LangGraph Design

## 1. Goal

Replace the current fixed-plan AIOps diagnostic graph with a bounded, evidence-driven loop that
can identify missing evidence, create a revised plan, reject an unsupported root-cause decision,
and produce a policy-classified recovery proposal.

The change targets `AiopsDiagnosticService`. It does not route Snapshot or Live benchmarks
through the general conversation Agent, and it does not change benchmark ground truth, score
weights, pass thresholds, or answer-isolation boundaries.

## 2. Current Problem

The current graph is:

```text
Planner -> Executor -> Evidence Evaluator -> Replanner -> Decision -> Report
```

The node named `Replanner` only checks whether the original fixed plan contains another step and
whether execution failed. It does not:

- decide whether the collected evidence is sufficient;
- describe unresolved evidence gaps;
- create replacement steps for competing hypotheses;
- validate trigger support or causal-chain grounding;
- separate root-cause diagnosis from recovery planning.

This is consistent with the final Live acceptance failures: the Agent can collect relevant facts
but still stop with an unsupported trigger, incomplete causal chain, or wrong primary mechanism.

## 3. Constraints

- Python 3.10+, LangGraph 1.2.8+, LangChain 1.3.12+, PostgreSQL persistence, and existing MCP
  contracts remain the runtime stack.
- No new dependency or external service.
- Ordinary CI stays offline and uses fake LLM, retrieval, MCP, and repository implementations.
- Real DashScope and CLS runs require separate explicit approval.
- Ground truth, scoring failures, private Live recovery handles, and oracle policy remain outside
  Agent state, prompts, RAG, reports, and checkpoints.
- The graph may require structure and evidence references, but it must not force a scenario's exact
  root cause, tool order, trigger, or answer label.
- A bounded loop must prevent repeated identical calls, cap all executor attempts including
  rejected duplicates and failures, and prevent unbounded model usage or replanning.
- Nginx remains proposal-only. No Nginx write, reload, restart, signal, or route switch is added.
- Existing PostgreSQL and Redis Live recovery policies remain the only authority for their scoped
  synthetic mutations.

## 4. Reuse Assessment

### 4.1 Existing project code

The implementation reuses:

- `AiopsDiagnosticService` and its persisted steps, evidence, checkpoints, audits, and SSE events;
- `DiagnosticPlanStep`, `HypothesisState`, `ObservationDecision`, and `RootCauseDecision`;
- discovered `McpToolDefinition` schemas and existing plan validation;
- trusted Live `SearchLog` argument binding and execution-time scope validation;
- existing Live recovery services and their deterministic authorization checks;
- existing Snapshot and Live adapters and artifact builders.

### 4.2 GitHub references

- `langchain-ai/langgraph` (MIT, active): conditional state-machine edges, bounded graph loops,
  persistence, and human-interrupt contracts. Directly reused through the dependency already in
  the project.
- `Tracer-Cloud/opensre` (Apache-2.0, active): evidence-gathering loop, explicit conclusion
  acceptance, iteration caps, and separation between the interactive assistant and investigation
  pipeline. Reference only; adopting it would introduce a second runtime and incompatible state
  model.
- Small LangGraph incident-response samples found in GitHub search were not adopted. Several have
  no license, and the licensed sample inspected is too small to provide a production contract.

### 4.3 Decision

Use wrapped adoption of the existing LangGraph primitives behind project-owned typed decisions.
Do not add a framework or copy external implementation code.

## 5. Selected Architecture

```text
Planner
  -> Evidence Executor
  -> Evidence Evaluator
  -> Sufficiency Gate
       | insufficient and budget remains
       v
     Replanner ----------------------+
       |                              |
       +--------> Evidence Executor --+

Sufficiency Gate
  -> Root Cause Decision
  -> Decision Validator
       | invalid and budget remains
       +--------> Replanner
       | valid or exhausted
       v
     Recovery Planner
  -> Policy Gate
  -> Report
```

The scorer remains outside the graph. `Sufficiency Gate` and `Decision Validator` judge public
evidence contracts, not correctness against an oracle.

## 6. State and Typed Decisions

### 6.1 Evidence sufficiency

Add a validated `EvidenceSufficiencyDecision` containing:

- `status`: `sufficient` or `insufficient`;
- `evidence_ids`: persisted evidence used by the assessment;
- `supported_hypotheses` and `refuted_hypotheses`: public hypothesis IDs only;
- `unresolved_hypotheses`: public hypothesis IDs only;
- `missing_evidence`: bounded public descriptions of unresolved observations;
- `recommended_tools`: discovered read/proposal tool names only;
- `summary`: a concise auditable explanation without private chain-of-thought.

The parser rejects unknown hypothesis IDs, unknown tool names, unpersisted evidence IDs, oversized
lists, and malformed status values.

### 6.2 Replanning state

Add:

- `replan_count`;
- `max_replans`, default `2`;
- `max_total_steps`, fixed at `6` across the initial plan and all replans;
- `executor_attempt_count`, incremented for every persisted executor attempt including rejected
  duplicates and failed tools;
- `executed_step_fingerprints` derived from normalized tool name and arguments;
- `evidence_sufficiency`;
- `decision_validation`;
- `termination_reason`.

The initial plan is capped at four steps and the total number of persisted executor attempts is
capped at six, preserving the existing `bounded_plan` scoring contract. Rejected duplicates and
failed tools consume this budget even when no MCP call is made. Replanning may append new steps but
cannot replay an identical tool-and-argument pair or reference an undiscovered tool.

The planner and report payloads persist `workflowVersion=evidence-driven-v2`. Artifact readers use
this marker to distinguish modern fail-closed validation from legacy records: a v2 decision is
valid only when a following validation step says `valid`; a missing validation in a partial or
interrupted v2 run invalidates the candidate.

### 6.3 Decision validation

Add a validated `RootCauseValidationDecision` containing:

- `status`: `valid` or `invalid`;
- `evidence_ids` supporting the validation;
- `unsupported_fields`: a subset of `component`, `mechanism`, `trigger`, and `causalChain`;
- `missing_evidence`;
- `summary`.

Deterministic checks run first: schema validity, canonical public vocabulary, persisted evidence
references, non-empty trigger, bounded causal chain, and confidence range. Semantic validation then
uses only the alert, public hypotheses, structured observations, hypothesis states, and bounded
evidence summaries. It never receives the scenario oracle or scorer output.

### 6.4 Recovery proposal

Add a validated `RecoveryPlan` containing:

- `mode`: `no_action`, `proposal_only`, `external_policy_required`, or `manual_review`;
- `action`, `target`, and `rationale`;
- an optional discovered proposal `tool` and schema-valid `arguments`;
- `risk` and `rollback`;
- at least two `verification_steps` when an action is proposed;
- `evidence_ids` and `decision_confidence`;
- `human_approval_required`.

This plan is an auditable recommendation, not authorization to mutate infrastructure.

### 6.5 Policy result

Add a deterministic `RecoveryPolicyDecision` containing:

- `status`: `allowed`, `denied`, or `deferred`;
- `authorization_code` from a bounded public vocabulary;
- `execution_permitted`: always `false` inside this change;
- `proposal_recorded`: whether a policy-approved, side-effect-free proposal tool completed;
- `human_approval_required`;
- `summary`.

PostgreSQL and Redis Live recovery remain outside the graph and continue to revalidate private
run-scoped state before mutation. A request-scoped tool-policy mapping identifies the existing
Nginx proposal tool as `proposal_only`; no tool is proposal-safe by name or model assertion alone.
The Nginx result remains proposal-only and cannot authorize an infrastructure execution.

## 7. Node Behavior

### 7.1 Planner

Keep SOP retrieval, MCP discovery, trusted argument binding, plan parsing, and initial evidence
persistence. Store the discovered tool contracts in bounded state so later replans use the same
execution boundary.

### 7.2 Evidence Executor and Evaluator

Keep one tool call per graph pass and persist the raw safe tool result before evaluation. Record a
stable step fingerprint. A failed tool call becomes evidence but does not automatically terminate
the investigation if another non-duplicate tool can fill the gap.

### 7.3 Sufficiency Gate

Run after every observation. It combines deterministic facts with a structured model assessment:

- whether at least one hypothesis is supported;
- whether material competing hypotheses remain unresolved;
- whether the primary trigger and causal path have direct evidence;
- whether additional discovered tools can resolve a named gap.

If evidence is insufficient and budget remains, route to `Replanner`. If sufficient, route to
`Root Cause Decision`. If no useful step remains or the budget is exhausted, route to Decision
with `termination_reason=evidence_budget_exhausted`; the final report must say evidence is
insufficient instead of inventing certainty.

### 7.4 Replanner

Generate only gap-targeted replacement steps from:

- the current sufficiency or validation failure;
- public hypotheses and hypothesis states;
- already executed fingerprints;
- discovered tool contracts;
- trusted tool arguments.

It may append at most the remaining execution budget. It cannot erase evidence, modify earlier
observations, or see scorer feedback.

### 7.5 Root Cause Decision and Validator

Keep the current structured decision and grounded fallback. The validator runs afterward. An
invalid decision routes back to `Replanner` if the failure identifies a resolvable evidence gap
and budget remains. Otherwise the decision is cleared and the report records an insufficient or
unsupported conclusion.

The validator does not compare with ground truth and therefore cannot manufacture a benchmark
pass.

### 7.6 Recovery Planner and Policy Gate

The Recovery Planner runs only after a valid root-cause decision. It produces a structured plan
using public evidence and decision fields. It may recommend a proposal-only or externally
authorized recovery but cannot invoke a write tool.

The Policy Gate deterministically denies malformed, unsupported, unscoped, or write-permitting
plans. It defers scenario-owned PostgreSQL and Redis actions to the existing Live recovery
services. Nginx is always proposal-only and human approval is required.

After authorization, the gate may materialize a proposal by calling only a discovered tool that
the request-scoped policy mapping classifies as `proposal_only`. The call still passes the MCP
schema validator and normal tool-call audit. This preserves the existing Nginx Live Recovery
contract without allowing a write: a model cannot classify a tool, an unclassified tool is denied,
and proposal materialization leaves `execution_permitted=false`.

### 7.7 Report

Include the sufficiency assessment, validated root-cause status, recovery plan, and policy result
in the report payload and checkpoint chain. The Markdown report distinguishes:

- confirmed diagnosis;
- evidence-limited diagnosis;
- proposed recovery;
- externally policy-controlled recovery;
- denied or human-approval-required action.

No private chain-of-thought or raw model response is persisted.

## 8. Failure and Loop Handling

- A malformed sufficiency, replanning, validation, or recovery response uses a bounded
  deterministic fallback and records a safe error category.
- Model failure does not silently mark evidence sufficient.
- Repeated plan fingerprints are rejected before execution.
- No useful new step, replan cap, or total-step cap ends the loop explicitly.
- Decision validation failure cannot fall through to a confident report.
- Recovery planning failure produces `manual_review`, never implicit execution.
- Existing API stream failure behavior and task ownership isolation remain unchanged.

## 9. Testing and Acceptance

### 9.1 Offline unit and service tests

Add tests proving:

1. insufficient evidence routes to a real replan with a new bounded step;
2. sufficient evidence bypasses replanning;
3. a failed tool may be replaced by a different useful tool;
4. identical tool-and-argument calls cannot repeat;
5. unknown tools, hypotheses, or evidence IDs are rejected;
6. invalid root-cause trigger or causal chain routes back to replanning;
7. unresolved validation after budget exhaustion clears the unsupported decision;
8. a valid decision reaches recovery planning and policy classification;
9. recovery plans never grant write execution inside the graph;
10. only a policy-mapped, schema-valid proposal tool may be materialized and audited;
11. Nginx remains proposal-only and requires human approval;
12. PostgreSQL and Redis actions remain controlled by existing Live policies;
13. steps, checkpoints, evidence links, audits, SSE events, and reports remain owner-scoped;
14. scorer and ground-truth files remain unreachable from prompts, RAG, Agent state, and reports;
15. v2 partial/interrupted artifacts without a valid decision-validation step fail closed while
    truly legacy artifacts remain readable;
16. owner-visible evidence chains include all new phases while a different owner is denied both
    chain reads and SSE subscription;
17. existing Snapshot/Live artifact parsing remains backward compatible.

### 9.2 Regression gates

- Targeted AIOps and Live adapter tests.
- Full ordinary backend pytest suite.
- Ruff.
- Strict Pyright.

No real LLM, CLS, or Docker run is required for implementation verification. A later paid Live
acceptance may run sequentially only after explicit approval.

## 10. Alternatives Rejected

### Only rename or enhance the current Replanner

This is smaller but leaves unsupported decisions and recovery proposals without an independent
gate. It would not address the observed trigger and causal-chain failures.

### Move all Live recovery into LangGraph

This would expose private run handles and mutation authority to Agent state, couple the production
diagnostic service to benchmark drivers, and weaken the existing deterministic recovery boundary.
It is rejected for this change.

### Replace the workflow with OpenSRE or another Agent runtime

This would duplicate the current LangGraph, persistence, MCP, RAG, and benchmark integration and
would make regression attribution harder. OpenSRE remains a reference, not a dependency.

## 11. Non-goals

- Conversation Agent integration or Chat-to-AIOps Eval.
- Automatic alert ingestion or webhook-triggered diagnosis.
- General human-approval UI or graph resume endpoint.
- New remediation write tools.
- Changing benchmark data or scoring to turn current failures into passes.
- Running paid or external acceptance tests during ordinary CI.
