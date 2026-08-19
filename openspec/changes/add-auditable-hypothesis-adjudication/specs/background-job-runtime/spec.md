## ADDED Requirements

### Requirement: AIOps jobs resume from durable execution state

Background job runtime SHALL resume an interrupted AIOps job only when task ID and graph version match. It SHALL
load the last durable LangGraph checkpoint and reuse completed execution records instead of restarting the graph.

#### Scenario: Worker lease expires after a checkpoint
- **WHEN** an AIOps Worker stops and another Worker claims the expired job lease
- **THEN** the new Worker MUST resume from the last completed checkpoint with the original budgets and deadlines

#### Scenario: SSE client disconnects
- **WHEN** the browser disconnects from an active AIOps event stream
- **THEN** the PostgreSQL-backed job MUST continue independently
- **AND** reconnect MUST replay durable events without creating a second diagnostic run

### Requirement: Unknown side effects are not blindly replayed

Background job runtime SHALL distinguish known failed work from side-effecting work with an unknown outcome.

#### Scenario: Recovery request loses its response
- **WHEN** a recovery tool request may have reached the target but its response is lost
- **THEN** execution MUST become `uncertain`
- **AND** retry MUST run an allowlisted state probe or require manual review instead of replaying the action

### Requirement: PostgreSQL remains the idempotency authority

Runtime SHALL use PostgreSQL unique constraints and conflict-safe reads as the final idempotency guarantee.

#### Scenario: Two Workers claim the same logical execution
- **WHEN** two Workers concurrently claim one stable execution key
- **THEN** at most one Worker MUST execute the operation
- **AND** the other Worker MUST wait for or reuse the durable result
