## ADDED Requirements

### Requirement: Order pool leak Live scenario is cross-source and reproducible

The system SHALL reproduce connection-pool exhaustion through an isolated order-api's real asyncpg
connection lifecycle and SHALL produce complementary Runtime and actual service-log evidence.

#### Scenario: Runtime alone does not reveal the exception lifecycle
- **WHEN** the Runtime Investigator inspects a saturated pool and PostgreSQL sessions
- **THEN** it SHALL prove pool exhaustion, request timeout, database reachability, and absence of lock waits
- **AND** it SHALL NOT return an Oracle field or connection-leak answer label

#### Scenario: CLS records originate from order-api events
- **WHEN** the CLS evidence preparer handles the order-pool scenario
- **THEN** uploaded records SHALL originate from the current run's order-api `/events` output
- **AND** evaluator-authored fault-answer templates MUST NOT be used

#### Scenario: The fault uses a real run-scoped order update
- **WHEN** the scenario establishes its baseline, injects the exception path, and verifies recovery
- **THEN** it SHALL execute parameterized updates against only the current run's test order
- **AND** Cleanup SHALL remove that order and all held connections

### Requirement: Single and Multi strategy comparison is fair

The system SHALL expose the same tools, trusted arguments, model, knowledge base, shared global budgets,
and scorer to Single and Multi strategies; only Runtime/Log investigation scheduling MAY differ.

#### Scenario: Multi investigators execute concurrently
- **WHEN** Runtime and Log Investigators claim step or model budget concurrently
- **THEN** they SHALL consume one atomic global budget rather than per-investigator copies
- **AND** budget exhaustion SHALL fail closed without adding unexecuted Evidence

### Requirement: Isolated recovery is scoped and idempotent

The system SHALL execute at most one restart of the isolated current-run order-api and SHALL independently
verify old-connection release, new-generation readiness, database health, and business recovery.

#### Scenario: A process fails after restart but before terminal persistence
- **WHEN** the same recovery intent is replayed after coordinator reconstruction
- **THEN** the system SHALL require manual review for an uncertain side effect
- **AND** it MUST NOT restart the service again

#### Scenario: Production semantics request recovery
- **WHEN** the diagnosis targets a non-isolated environment
- **THEN** the system SHALL produce a human-approval proposal only
- **AND** it MUST NOT execute restart or rollback
