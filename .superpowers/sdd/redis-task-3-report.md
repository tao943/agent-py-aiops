# Redis Task 3 Report: Streams publisher and Outbox dispatcher

## Scope delivered

- Added `RedisStreamJobEventPublisher`, which atomically uses Lua to acquire a
  per-event TTL dedupe key and `XADD` one sanitized event to
  `<prefix>:aiops:events` with approximate bounded retention.
- The stream fields are `event_id`, SHA-256 `owner_id_hash`, `job_id`, decimal
  `sequence`, non-empty `event_type`, canonical JSON `payload`, and
  `created_at`. Payload redaction recursively removes credential and header
  values, including camelCase `apiKey`.
- Added the `JobEventPublisher` protocol and `OutboxDispatcher` with bounded
  claiming, acknowledgement-before-publish-state, per-event failure release,
  capped exponential backoff, poison-event continuation, cancellation release,
  and idempotent `start`/awaited `stop` lifecycle handling.
- No application, API, or SSE wiring was changed.

## TDD evidence

- Initial RED: `pytest tests/test_redis_streams.py tests/test_outbox_dispatcher.py -q`
  failed collection because `super_ai.redis_runtime.streams` and
  `super_ai.events` did not exist.
- First GREEN: the minimal Redis publication and successful dispatcher
  acknowledgement path passed (`2 passed`).
- Dispatcher RED: failure/backoff, poison continuation, cancellation release,
  and lifecycle tests failed (four failures), then passed after the dispatcher
  implementation (`5 passed`).
- Redis RED: empty `event_type` did not raise `ValueError`; the publisher now
  rejects it before Lua execution. A subsequent redaction RED showed nested
  camelCase `apiKey` was retained; normalization now redacts it.

## Verification

- `python -m pytest tests/test_redis_streams.py tests/test_outbox_dispatcher.py -q`
  passed repeatedly: `9 passed`, using `redis://localhost:6379/15` and a unique
  `task3-<uuid>` prefix per test.
- Each Redis test cleanup scans and deletes only `<exact-prefix>:*`; no
  `FLUSHALL`, database `/0`, or another test prefix is touched.
- `python -m ruff check .` passed.
- Scoped strict Pyright for the Task 3 source and tests passed. Full-repository
  Pyright retains one pre-existing error in
  `tests/test_postgresql_background_jobs.py:69` (`BackgroundJobRecord | None`
  optional member access), already documented in the Task 2 report.
- `git diff --check` passed.

## Concern

`redis-py`'s Stream response stubs are incomplete under strict Pyright. The
real Redis integration test uses narrowly scoped per-file suppressions for its
unknown library member/response types; production code remains strict-clean.

## Follow-up review hardening

- The Lua script now checks an existing dedupe key, performs `XADD`, and sets
  the dedupe TTL only after `XADD` succeeds. A real Redis test pre-sets the
  stream key to a string, proves the failed `XADD` leaves no dedupe key, then
  removes the bad key and confirms exactly one retry delivery.
- Recursive redaction now recognizes normalized `privateKey`/`private_key`,
  `accessKeyId`/`access_key_id`, `secretId`, `secretKey`, and `clientSecret`
  fields in addition to the previously tested credentials. Ordinary
  `ordinary_key` data remains untouched.
- Repository release failures are logged without payloads, do not block later
  claimed events, and cannot replace the original `CancelledError`.

## Credential-name normalization follow-up

- Field normalization now preserves acronym boundaries before converting
  separators to snake case. It recognizes `APIKey`, `API_KEY`,
  `AWSAccessKeyId`, `AccessKeyID`, `PRIVATE_KEY`, and `CLIENT_SECRET` in nested
  dictionaries and lists.
- Redaction now uses only exact sensitive names and documented credential
  suffixes. It intentionally preserves ordinary fields such as `tokenCount`
  and `secretaryName`.

## Credential-container follow-up

- Exact plural `credentials` and `headers` fields are now redacted as whole
  containers, so nested raw values cannot survive transport serialization.
- The explicit `_access_key` suffix covers `AWS_SECRET_ACCESS_KEY`,
  `SecretAccessKey`, and `secret_access_key` without introducing a generic
  `key` match.
