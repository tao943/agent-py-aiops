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
