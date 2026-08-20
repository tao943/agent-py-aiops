## ADDED Requirements

### Requirement: Investigation strategy is deterministic and auditable

Workflow SHALL select `deterministic_fast_path`, `single_agent`, or `multi_agent` from current-task
public state, versioned policy, capability availability, remaining time, and remaining model budget.
It MUST NOT read scenario identity, run identity, Oracle, Ground Truth, score rules, or fixture values.

#### Scenario: Complexity is below the multi-agent threshold
- **WHEN** no hard gate requires stopping and the public route score is between 0 and 3
- **THEN** Workflow SHALL select `single_agent`
- **AND** it SHALL persist the policy version, score, and stable reason codes

#### Scenario: Deterministic evidence is already sufficient
- **WHEN** a current-task trusted pattern has closed the active hypotheses or `decisionReady` is true
- **THEN** Workflow SHALL select `deterministic_fast_path`
- **AND** it MUST NOT start another Executor or Investigator Dispatch
- **AND** it SHALL continue through Sufficiency, Decision, Validator, Recovery, Policy, and Report

#### Scenario: Multiple independent public sources are required
- **WHEN** the score is at least 6 and at least two unfinished trusted source Dispatches pass all time and budget gates
- **THEN** Workflow MAY select `multi_agent`
- **AND** selected Investigator types SHALL have a stable order

### Requirement: Parallel investigators have isolated capabilities

Runtime and Log Investigators SHALL receive only source-scoped read-only tools and MUST return a
schema-valid EvidencePacket without mutating shared Fact, Hypothesis, or Observation state.

#### Scenario: Runtime and Log steps are independent
- **WHEN** a Multi route selects trusted unfinished Runtime and Log Dispatches
- **THEN** Workflow MAY execute them concurrently
- **AND** each branch SHALL write only its own Packet, Evidence, audit, checkpoint, and progress event

#### Scenario: An Investigator attempts a recovery action
- **WHEN** a plan step names a recovery, proposal-only, external-policy, or otherwise writable tool
- **THEN** the capability registry MUST reject the step
- **AND** the tool MUST NOT be dispatched

### Requirement: Multi-agent tools require explicit read-only trust

Discovery alone MUST NOT classify an MCP tool as safe. Only a code-owned capability descriptor with
`read_only=true` MAY expose a discovered tool to an Investigator; unknown tools SHALL fail closed.

#### Scenario: A user MCP server exposes an unknown tool
- **WHEN** discovery returns a tool without a matching trusted capability descriptor
- **THEN** Workflow MUST NOT classify it as Runtime or Log
- **AND** Multi-Agent routing MUST NOT dispatch it

#### Scenario: Model output forges a source domain
- **WHEN** a Planner step reports a source domain that differs from the trusted tool registry
- **THEN** Workflow SHALL replace or reject the reported domain using the registry
- **AND** the model-provided domain MUST NOT expand tool access

### Requirement: Evidence aggregation is deterministic and single-writer

Workflow SHALL validate Packet ownership, completed tool audit, Evidence quality, temporal scope,
deduplication, and stable ordering before one Aggregator writes shared diagnostic state.

#### Scenario: Parallel completion order changes
- **WHEN** the same Runtime and Log Packets arrive in different coroutine completion orders
- **THEN** Aggregator SHALL produce identical Facts, Observations, Hypothesis states, and Evidence order

#### Scenario: One Evidence is cited by multiple Packets
- **WHEN** multiple claims cite the same current-task Evidence
- **THEN** Aggregator MUST NOT count it as independent duplicate evidence

#### Scenario: Incident state differs from current health
- **WHEN** one direct claim describes an incident-window failure and another describes current health
- **THEN** Aggregator MUST preserve both time scopes
- **AND** it MUST NOT automatically classify them as conflicting

### Requirement: Multi-agent dispatch is resumable and fail-closed

Completed Dispatches SHALL be reused by stable dispatch key. Timeout, partial failure, late result,
and all-failed paths MUST NOT be interpreted as negative evidence.

#### Scenario: Worker restarts after one branch completes
- **WHEN** one Dispatch is completed and another is unfinished at restart
- **THEN** Workflow SHALL reuse the completed Packet
- **AND** it SHALL resume only the unfinished logical Dispatch without duplicating tool calls or Evidence

#### Scenario: One Investigator times out
- **WHEN** one Packet completes and another times out
- **THEN** Workflow SHALL aggregate the completed Packet
- **AND** timeout SHALL be recorded as a limitation rather than a refuting claim

#### Scenario: Every Investigator fails
- **WHEN** all selected Dispatches fail and safe Single steps remain within budget
- **THEN** Workflow SHALL record `fallback_to_single_agent`
- **AND** it SHALL execute only unfinished contract-valid steps

#### Scenario: A result arrives after decision readiness
- **WHEN** an already-issued Dispatch completes after the task has become decision-ready
- **THEN** Workflow SHALL record `late_result_ignored`
- **AND** it MUST NOT modify Decision, Report, recovery authorization, or scoreable Artifact state

### Requirement: Graph topology versions isolate resumable state

The new investigation topology SHALL use graph version `aiops-diagnostic-v3`. A v2 checkpoint MUST
only resume with the legacy topology and MUST NOT be injected into v3 channels.

#### Scenario: An unfinished v2 task is resumed after deployment
- **WHEN** a task owns an `aiops-diagnostic-v2` checkpoint
- **THEN** Workflow SHALL select the legacy graph and v2 thread namespace
- **AND** it MUST NOT query or write the v3 checkpoint namespace for that execution

#### Scenario: A new task starts on the investigation topology
- **WHEN** no historical graph version is persisted for a newly accepted task
- **THEN** Workflow SHALL persist `aiops-diagnostic-v3` before graph execution
- **AND** all node, model, tool, checkpoint, and Artifact identities SHALL use v3
