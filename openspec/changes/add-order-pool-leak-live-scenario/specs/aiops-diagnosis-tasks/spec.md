## ADDED Requirements

### Requirement: Cross-source investigation preserves evidence boundaries

Runtime and Log Investigators SHALL emit bounded EvidencePackets and SHALL NOT write shared hypotheses,
decisions, recovery authorization, or evaluator-private state.

#### Scenario: Runtime and Log evidence are each incomplete
- **WHEN** only Runtime evidence or only order-api lifecycle logs are available
- **THEN** the diagnosis SHALL record the supported partial facts and limitations
- **AND** it MUST NOT claim a complete exception-path connection-leak causal chain

#### Scenario: The Aggregator receives both sources
- **WHEN** scoped Runtime and CLS packets agree on run, incident, time window, and generation
- **THEN** the Aggregator MAY construct the checkout-without-checkin to pool-saturation causal chain
- **AND** every causal claim SHALL cite the supporting packet identifiers

### Requirement: Order-pool diagnostic tools are read-only and answer-isolated

The trusted order-pool Runtime server SHALL expose only scoped pool state, scoped database sessions,
database reachability, and business-probe outcome.

#### Scenario: A tool output is persisted or shown to the Agent
- **WHEN** the order-pool Runtime client returns an observation
- **THEN** it MUST NOT contain credentials, raw SQL, PIDs, fault tokens, Oracle fields, Ground Truth,
  `primary_cause`, or a connection-leak answer label

#### Scenario: A strategy is forced by the internal Benchmark CLI
- **WHEN** either `single` or `multi` is requested
- **THEN** both strategies SHALL receive the same discovered tool catalog and trusted argument bindings
- **AND** ordinary diagnosis API behavior SHALL remain service-owned `auto`

### Requirement: Order-pool Single diagnosis has a trusted deterministic closure

The Workflow SHALL support `order_connection_lifecycle_failure` only when current-task trusted
evidence establishes checkout, failed update, acquire timeout, missing checkin, saturated pool,
zero free connections, an observed waiter, current scoped PostgreSQL sessions, database reachability,
absence of lock wait, and a timed-out business acquisition probe.

#### Scenario: The complete compound pattern is present
- **WHEN** every required lifecycle, Runtime, database, rule-out, and impact fact is present and consistent
- **THEN** Workflow SHALL support `order_connection_lifecycle_failure`
- **AND** it SHALL refute database-unreachable and database-lock-wait alternatives
- **AND** trigger, mechanism, and impact SHALL cite independent trusted source groups

#### Scenario: A required fact is missing, conflicting, foreign, or duplicated
- **WHEN** any required fact is absent or contradictory, belongs to another owner or task, or reuses an existing source fingerprint under another Evidence ID
- **THEN** the compound resolver MUST fail closed
- **AND** it MUST NOT create a supported lifecycle hypothesis or grounded recovery authorization

### Requirement: Order-pool Specialists preserve immutable scope and bounded behavior

Runtime and Log Specialists SHALL receive an immutable public assignment, SHALL have isolated local
state, and SHALL run at most one Local Planning role, three read-only tool steps, and one Evidence
Analysis role. Each structured role MAY make one format-correction retry under the same logical call.

#### Scenario: A Log Local Plan changes its prepared query scope
- **WHEN** model output changes Region, TopicId, time window, run, incident, owner, Query, or Limit
- **THEN** Workflow SHALL reject the arguments before MCP invocation
- **AND** it SHALL retain the exact code-owned trusted argument binding

#### Scenario: A Specialist completes model analysis
- **WHEN** a Specialist produces a schema-valid result
- **THEN** only public facts, Evidence IDs, tested hypotheses, limitations, counters, and stable checksums SHALL be persisted
- **AND** raw prompts, raw model responses, credentials, and private reasoning MUST NOT be persisted

### Requirement: Order-pool Multi aggregation is deterministic and non-decisional

The Aggregator SHALL validate owner, task, role, tool audit, Evidence provenance, source fingerprint,
temporal scope, budget use, and stable result checksum without calling an LLM or voting on a cause.

#### Scenario: Specialist completion order changes
- **WHEN** identical Runtime and Log results arrive in either completion order
- **THEN** aggregation checksum, normalized facts, source groups, conflicts, and missing domains SHALL be identical
- **AND** the Aggregator MUST NOT create a RootCauseDecision or authorize recovery

#### Scenario: One Specialist fails or times out
- **WHEN** one role has completed Evidence and the other terminates unsuccessfully
- **THEN** Workflow SHALL preserve the completed Evidence and record the missing domain
- **AND** incomplete closure MUST prevent unsafe automatic recovery

### Requirement: Order-pool Multi release is benchmark-forced then shadow-only

The first release SHALL permit effective Multi execution only for the internal Order Pool Benchmark.
Ordinary `auto` requests SHALL persist the candidate score and reasons but SHALL execute Single.

#### Scenario: Forced Multi fails
- **WHEN** the internal Benchmark requested Multi and either Specialist or aggregation fails
- **THEN** the terminal result SHALL remain a Multi failure
- **AND** Workflow MUST NOT silently execute Single to replace or overwrite that result

#### Scenario: Auto identifies a Multi candidate
- **WHEN** the public route score is at least 5 and all capability, budget, and deadline gates pass
- **THEN** Workflow SHALL persist a shadow Multi candidate
- **AND** the effective production strategy SHALL remain Single until separately approved

### Requirement: Specialist replay is PostgreSQL-authoritative

Completed role, tool, Evidence, and aggregation identities SHALL be recoverable from PostgreSQL and
SHALL be charged or applied at most once per logical identity.

#### Scenario: A worker restarts after a role or tool completed
- **WHEN** the same task, graph version, specialist role, logical role or step, and input fingerprint are replayed
- **THEN** Workflow SHALL reuse the persisted completed result
- **AND** it MUST NOT repeat the model call, tool call, Evidence append, or model-budget charge
