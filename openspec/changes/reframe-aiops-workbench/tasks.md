## 1. Contracts and incident projection

- [ ] 1.1 Add shared Incident, runtime status and Agent configuration contracts with strict fixtures.
- [ ] 1.2 Implement owner-scoped Incident list/detail projection, stable opaque cursor pagination and formal Recovery Intent selection.
- [ ] 1.3 Cover cross-owner isolation, equal-timestamp pagination, legacy recovery exclusion and safe public serialization.

## 2. Event-first Vue workspace

- [ ] 2.1 Add design tokens, accessible primitives and the responsive event-first application Shell.
- [ ] 2.2 Route `/` to `/incidents`, add all primary workspaces and preserve safe redirects from legacy routes.
- [ ] 2.3 Implement the real Incident queue with loading, empty, error, partial and pagination states.
- [ ] 2.4 Implement the investigation workspace with evidence timeline, diagnosis/report projections and formal Recovery Intent controls.
- [ ] 2.5 Add bounded visible-page recovery refresh with stale/error retention and terminal stop behavior.

## 3. Agent configuration lifecycle

- [ ] 3.1 Add Alembic revision `202608230002`, repositories and owner-scoped resource/version/binding/audit services.
- [ ] 3.2 Compatibly migrate existing Chat Prompt/Skill data without changing owner scope or losing selections.
- [ ] 3.3 Implement create draft, edit, validate, publish, deprecate, bind and audit APIs with mutation-level authorization.
- [ ] 3.4 Assemble immutable runtime snapshots with mandatory safety prompt first, tool allowlist intersection and required Policy Gate.
- [ ] 3.5 Build the Agent configuration UI with dirty-draft protection, capability-aware controls and explicit publish/binding state.

## 4. Unified workspaces

- [ ] 4.1 Move Chat to `/assistant`, remove persistent Prompt/Skill sidebars and consume only published bound snapshots.
- [ ] 4.2 Align knowledge, integrations and system status pages with the shared Shell and real API state.
- [ ] 4.3 Add responsive narrow layout, keyboard focus, reduced motion and desktop/narrow visual smoke coverage.

## 5. Acceptance and documentation

- [ ] 5.1 Pass focused contracts, backend repository/API/security and frontend store/component tests.
- [ ] 5.2 Pass frontend typecheck/build, Ruff, Pyright, strict OpenSpec and docs build gates proportionate to changed files.
- [ ] 5.3 Run the local application with real APIs and capture desktop and narrow screenshots without credentials or sensitive runtime data.
- [ ] 5.4 Sync WIKI/current architecture documentation and record the final verified route and recovery behavior.
