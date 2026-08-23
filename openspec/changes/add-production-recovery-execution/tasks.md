## 1. Contracts and persistence

- [x] 1.1 Add shared recovery contracts, stable errors, domain types and safe public projections.
- [x] 1.2 Add disabled-by-default recovery configuration and unique diagnostic selectors.
- [x] 1.3 Add Alembic revision `202608230001`, ORM models and owner-scoped repository protocols.
- [x] 1.4 Implement conflict-safe repository transitions and atomic Intent/Job/Event units of work.

## 2. Grounded intent and policy

- [x] 2.1 Deterministically map validated component/mechanism/evidence to one configured action target.
- [x] 2.2 Create immutable Intent from persisted owner-scoped facts and reject client/model execution parameters.
- [x] 2.3 Implement creation and execution-time policy gates, approval fingerprint binding and 600-second TTL.

## 3. Execution and verification

- [x] 3.1 Implement allowlisted Compose preflight, one-shot restart and four-signal verification.
- [x] 3.2 Implement PostgreSQL fresh blocker probe, approved parameterized termination and verification.
- [x] 3.3 Register the durable recovery handler with at-most-once execution and restart-safe verification resume.

## 4. API and compatibility

- [x] 4.1 Add owner-scoped create/get/approve/reject/cancel/events APIs and recovery rate limit.
- [x] 4.2 Route confirmed Chat recovery requests to formal Intent without granting approve/execute capability.
- [x] 4.3 Preserve legacy request-only rows as read-only and permanently non-executable.

## 5. Acceptance and documentation

- [x] 5.1 Pass contract, migration, repository, policy, executor, Worker, API and security tests.
- [x] 5.2 Pass isolated real Compose auto-recovery and approved PostgreSQL blocker acceptance.
- [ ] 5.3 Update frontend implementation plan to consume full recovery API and use migration `202608230002` for Agent configuration.
- [ ] 5.4 Sync operator documentation, run strict OpenSpec/Ruff/Pyright/focused pytest gates and record verification evidence.
