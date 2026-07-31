# Redis Task 5 Report: lifecycle and degraded readiness

## Scope delivered

- Production application composition now creates one lazy Redis client, stream
  publisher, Outbox dispatcher, per-instance SSE relay, and composed job-event
  subscriber when Redis configuration is valid.  No Redis network connection is
  made at module import.
- FastAPI lifespan starts the existing PostgreSQL background-job runtime and
  then starts the dispatcher and relay.  Redis lifecycle failures are recorded
  as sanitized degraded infrastructure and never prevent PostgreSQL runtime
  startup.
- Shutdown awaits relay and dispatcher cancellation before closing only the
  Redis client the application created; injected clients remain caller-owned.
- `/ready` and `/config/check` now expose an explicit Redis dependency.
  Redis-only failure reports `status: degraded` with HTTP 200 and a concise
  `Redis is unavailable.` error. PostgreSQL and the existing blocking
  dependencies retain their HTTP 503 behavior.
- Diagnostic creation and the PostgreSQL-canonical AIOps SSE route remain
  covered with an injected unavailable Redis client. Redis is not used as an
  event source of truth.

## TDD evidence

- Initial RED: the new lifecycle tests failed because `create_app` did not
  accept Redis settings/client/lifecycle injection; readiness RED failed for
  the missing `redis_client` composition boundary.
- Green: lifecycle ownership, exactly-once start/stop, relay startup failure
  isolation, Redis-only readiness degradation, and PostgreSQL failure behavior
  pass with injected fakes.

## Verification

Run from `apps/backend` using the checked-in `.venv` and a writable pytest
temporary directory:

- `python -m pytest tests/test_redis_lifecycle.py tests/test_readiness_api.py tests/test_aiops_sse_delivery.py tests/test_aiops_diagnostics.py tests/test_outbox_dispatcher.py tests/test_redis_streams.py -q` — 39 passed.
- `python -m ruff check src/super_ai/api/app.py src/super_ai/events tests/test_redis_lifecycle.py tests/test_readiness_api.py tests/test_aiops_sse_delivery.py tests/test_aiops_diagnostics.py` — passed.
- `python -m pyright src/super_ai/api/app.py src/super_ai/events tests/test_redis_lifecycle.py tests/test_readiness_api.py tests/test_aiops_sse_delivery.py tests/test_aiops_diagnostics.py` — 0 errors.
- `git diff --check` — passed.

`uv run` could not be used in this Windows environment because resolving the
existing dependency set attempts to build `python-snappy` without Microsoft C++
Build Tools. Docker smoke checks are unavailable because the Docker Desktop
npipe endpoint returns permission denied. Task 1–4 retain their prior real
Redis/PostgreSQL verification evidence; no Docker retry was attempted here.
