## ADDED Requirements

### Requirement: Hypothesis disposition is evidence-audited

Workflow SHALL represent every public hypothesis as `supported`, `refuted`, `causally_inactive`, or
`unresolved`. A `refuted` or `causally_inactive` disposition MUST cite current-task public Evidence.

#### Scenario: A complete cause does not dismiss a competitor
- **WHEN** one supported cause explains the incident but no evidence addresses another competitor
- **THEN** the competitor MUST remain `unresolved`

#### Scenario: Exactly one cause is sufficient
- **WHEN** exactly one hypothesis is supported and every active competitor is grounded as refuted or causally inactive
- **THEN** Workflow MAY form a single root-cause decision

#### Scenario: Multiple causes remain supported
- **WHEN** more than one public hypothesis remains supported under the v4 single-root schema
- **THEN** Workflow MUST fail closed to manual review

### Requirement: Deterministic adjudication uses trusted public rules

Workflow SHALL derive bounded typed facts only from secret-filtered public Observation fields. Planner-proposed
deterministic rules MUST instantiate a code-owned trusted template and MUST NOT define an arbitrary fact-to-disposition mapping.

#### Scenario: Planner proposes an unauthorized causal mapping
- **WHEN** a rule uses valid public facts but its template does not authorize that hypothesis and disposition
- **THEN** Workflow MUST NOT close the hypothesis deterministically
- **AND** Workflow MUST treat the rule as unresolved input for the bounded Adjudicator path

#### Scenario: Observation contains a secret-shaped field
- **WHEN** a tool result contains password, token, authorization, secret, or API-key shaped fields
- **THEN** Fact Adapter MUST omit those fields from facts, checkpoints, prompts, and public API payloads

### Requirement: LLM Adjudicator output is structured and safely diagnosable

Workflow SHALL request the provider-supported structured-output contract for each bounded Adjudicator batch.
It SHALL validate the returned hypothesis IDs, Evidence IDs, complete batch coverage, transition legality, and
causal coverage locally. It SHALL persist only allowlisted rejection codes and MUST NOT persist raw model output,
provider exception text, hidden reasoning, or response fragments.

#### Scenario: Model returns a syntactically valid but ungrounded batch
- **WHEN** an Adjudicator response references an unknown hypothesis or Evidence ID
- **THEN** Workflow MUST reject the complete batch without applying a partial transition
- **AND** it MUST record `unknown_identifier` and `incomplete_batch` without recording model content

#### Scenario: Format correction cannot complete before the deadline
- **WHEN** the first response is rejected and the soft deadline prevents the correction attempt
- **THEN** Workflow MUST record `soft_deadline_exceeded` and fail closed without a root-cause decision

### Requirement: Validation is deterministic first and risk-routed

Workflow SHALL always run Deterministic Validator. It SHALL call LLM Validator only when a code-computed risk
condition requires semantic adjudication, automatic recovery review, L2/L3 review, cross-component causality,
or high-quality conflict review.

#### Scenario: Pure deterministic evidence closes the case
- **WHEN** deterministic evidence produces one supported cause, grounded closed alternatives, and manual-only recovery
- **THEN** Workflow MUST skip LLM Validator and record allowlisted skip reasons

#### Scenario: Deterministic validation fails
- **WHEN** deterministic checks reject a candidate and no unused targeted replan is eligible
- **THEN** Workflow MUST create a deterministic manual-review plan
- **AND** Workflow MUST NOT spend an LLM Recovery Planner or Validator call

### Requirement: Compound trusted patterns remain answer-isolated

Workflow SHALL close multiple hypotheses deterministically only when a code-owned compound pattern matches
current-task public Evidence. The pattern MUST NOT read scenario identity, run identity, Oracle, Ground Truth,
score rules, or fixture values.

#### Scenario: Nginx timeout facts close differentiated alternatives
- **WHEN** one request has HTTP 504, upstream connect success, read deadline elapsed, independently healthy upstream and gateway probes, and an incident-scoped upstream-timeout event
- **THEN** Workflow SHALL support upstream response timeout
- **AND** every closed competitor MUST cite the direct current-task Evidence that closes it

#### Scenario: PostgreSQL row-lock facts form a cross-tool causal chain
- **WHEN** current-task public Evidence confirms a PostgreSQL Lock wait, a blocker-to-waiter edge, database reachability, a timed-out business probe, and matching contention and timeout log events
- **THEN** Workflow SHALL support PostgreSQL row-lock blocking
- **AND** it SHALL refute connectivity failure and an unlocked slow query using their direct counter-evidence
- **AND** it SHALL project separate trigger, mechanism, and impact observations

#### Scenario: One required fact is absent or belongs to another task
- **WHEN** any required fact is absent, contradicted, or its Evidence ID is not owned by the current task
- **THEN** Workflow MUST leave the affected hypothesis unresolved or conflicting
- **AND** it MUST NOT infer the benchmark answer from scenario identity

### Requirement: Replanner model calls require a provably useful search space

Before calling the Replanner model, Workflow SHALL determine from discovered tool schemas, trusted argument
contracts, execution-owned bindings, and causal capabilities whether at least one unexecuted contract-valid step
can still address the current evidence gap. If no such step can exist under bounded trusted arguments, Workflow
MUST persist `no_useful_step` without spending a Replanner model call.

#### Scenario: Bounded diagnostic calls are exhausted
- **WHEN** every gap-relevant tool has only empty, constant, or execution-owned arguments and every canonical call has already completed
- **THEN** Workflow MUST skip the Replanner model and record an allowlisted skip reason

#### Scenario: A free legal parameter space remains
- **WHEN** a gap-relevant tool accepts a legal parameter that code cannot prove exhausted
- **THEN** Workflow MAY call the bounded Replanner model once

### Requirement: Model and time budgets survive restart

Workflow SHALL limit all model requests, including retries and format corrections, to eight calls. It SHALL persist
model-call count, role audit, one-Replanner limit, five-minute soft deadline, and eight-minute hard deadline in graph state.

#### Scenario: Worker resumes a partially used budget
- **WHEN** a new Worker resumes the same task and graph version from PostgreSQL
- **THEN** remaining call and time budgets MUST be calculated from the persisted original values
- **AND** restart MUST NOT reset model count, Replanner count, or deadlines

#### Scenario: Hard deadline expires
- **WHEN** the persisted hard deadline has passed
- **THEN** Workflow MUST stop new model and tool work and persist only a safe terminal state and template report

### Requirement: Diagnostic side effects are idempotent

Workflow SHALL use stable IDs and conflict-safe persistence for initial alert Evidence, diagnostic Steps and Evidence,
tool audits, report links, model executions, tool executions, recovery intents, and audit events.

#### Scenario: Crash follows an evidence commit
- **WHEN** Evidence is committed but execution completion or graph checkpoint persistence fails before acknowledgement
- **THEN** resumed execution MUST reuse the same stable Evidence identity
- **AND** the evidence chain MUST NOT contain a duplicate logical record
