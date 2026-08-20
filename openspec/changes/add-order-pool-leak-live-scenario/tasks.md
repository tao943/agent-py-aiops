## 1. Contracts and isolated service

- [ ] 1.1 Add the order-pool Live scenario delta contracts and strict validation.
- [ ] 1.2 Implement the environment-only, fail-closed order-api app factory.
- [ ] 1.3 Implement run-scoped test orders, real asyncpg updates, bounded pool exhaustion, and safe events.
- [ ] 1.4 Add the isolated Compose service and Docker health contract.

## 2. Live runtime and evidence

- [ ] 2.1 Implement Driver, Runtime evidence, recovery, verification, cleanup, and audit.
- [ ] 2.2 Upload only actual order-api events through a validated CLS record provider.
- [ ] 2.3 Expose equal read-only Runtime and CLS tools to Single and Multi strategies.
- [ ] 2.4 Enforce one shared concurrent step/model budget across investigators.

## 3. Benchmark and safety

- [ ] 3.1 Add the public scenario, private Ground Truth, evidence, rule-out, semantic, and scoring contracts.
- [ ] 3.2 Register CLI runtime and scoped recovery without exposing a restart tool to the Agent.
- [ ] 3.3 Verify checkpoint/recovery replay, including the uncertain post-side-effect crash window.
- [ ] 3.4 Verify Ground Truth isolation, path safety, cross-run evidence rejection, and fail-closed behavior.

## 4. Acceptance

- [ ] 4.1 Pass the opt-in Docker injection, actual-event provider, recovery, Verify, and Cleanup contract.
- [ ] 4.2 Pass targeted pytest, Ruff, Pyright, Compose config, and strict OpenSpec validation.
- [ ] 4.3 Record implementation evidence without claiming Multi capability gain.
- [ ] 4.4 After separate user approval, run and persist the real 3×3 Single/Multi LLM+CLS comparison.
