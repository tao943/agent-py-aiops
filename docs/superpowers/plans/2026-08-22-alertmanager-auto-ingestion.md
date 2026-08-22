# Alertmanager Auto Ingestion Implementation Plan

> **For the primary agent:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. After the completed plan-review gate, the primary Agent implements and verifies alone; do not spawn implementation or review subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reliable Alertmanager v4 webhook that atomically creates one incident and one durable AIOps diagnostic job per active `groupKey` lifecycle.

**Architecture:** A focused `super_ai.alert_ingestion` package parses and minimizes untrusted deliveries, applies source-bound authorization and filters, then drives a PostgreSQL-backed incident state machine. Redis is an optional short lease only; the FastAPI route returns after commit and wakes the existing background runtime without waiting for LangGraph.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic 2, SQLAlchemy 2, asyncpg, PostgreSQL 16, Redis 7, Alembic, pytest, Ruff, Pyright, Nginx 1.30, Alertmanager 0.28.1.

## Global Constraints

- Do not add dependencies, external services, native binaries, or code with incompatible/unclear licensing.
- Reuse the existing PostgreSQL session factory, diagnostic models, background jobs, Redis client, Nginx, Alertmanager, and `aiops_diagnosis` handler.
- The endpoint is exactly `POST /aiops/alerts/webhook/alertmanager/{source_id}`.
- Body limit is 262144 bytes and delivery alert limit is 50; enforce both declared and actually read sizes.
- Persist only allowlisted fields and raw payload SHA-256; never persist or log the token, raw body, raw `groupKey`, unknown fields, or unsafe annotation values.
- Source configuration, never the payload, supplies `ownerUserId` and `knowledgeBaseId`; require `knowledgeBaseId == "kb_" + ownerUserId`.
- PostgreSQL is the final idempotency authority. Redis failure must degrade to PostgreSQL and still return 202 after a successful commit.
- One active incident, diagnostic task, and background job per `(owner_user_id, source_id, group_key_hash)` lifecycle.
- Duplicate firing updates the incident but starts no Agent; resolved closes the incident but never cancels or adds an LLM call.
- Automatic ingestion must not set `executionPermitted` or bypass the existing recovery Policy Gate.
- Webhook responses never wait for Agent, LLM, CLS, RAG, or report completion.
- Alertmanager groups by `alertname`, `service`, `environment`, and `run_id`; firing and resolved deliveries for one lifecycle use the identical grouping label set.
- Production source configuration remains disabled until the service account, knowledge base, and secret are provisioned.
- Use TDD for every production behavior and run focused tests only; no full pytest or full Benchmark is required.

## Reuse Assessment

- **Direct adoption:** existing Alertmanager container, SQLAlchemy/asyncpg repositories, durable background job runtime, Redis client, Nginx gateway, and LangGraph diagnosis handler.
- **Wrapped adoption:** wrap the existing Redis client behind a narrow lease protocol and create diagnostic/job rows in the ingestion transaction rather than calling repositories that commit independently.
- **Reference only:** `prometheus/alertmanager` Apache-2.0 Webhook v4 schema, 2xx acknowledgment, and retry semantics.
- **Rejected:** Grafana OnCall (AGPL-3.0 and too heavy) and Keep (license not asserted and too heavy).
- **Custom:** the small source adapter, minimized payload model, incident state machine, and atomic repository; no new dependency.

## File Map

- Create `apps/backend/src/super_ai/alert_ingestion/domain.py`: immutable records, enums, errors, repository/service result types.
- Create `apps/backend/src/super_ai/alert_ingestion/alertmanager.py`: v4 parsing, SHA-256, allowlisting, truncation, origin normalization.
- Create `apps/backend/src/super_ai/alert_ingestion/config.py`: source-bound configuration and environment-token validation.
- Create `apps/backend/src/super_ai/alert_ingestion/repositories.py`: atomic repository protocol.
- Create `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`: PostgreSQL state transitions and diagnostic/job creation.
- Create `apps/backend/src/super_ai/alert_ingestion/redis_runtime.py`: optional short lease with compare-delete.
- Create `apps/backend/src/super_ai/alert_ingestion/metrics.py`: bounded in-process counters and latency aggregates.
- Create `apps/backend/src/super_ai/alert_ingestion/service.py`: filter/lease/repository orchestration.
- Create `apps/backend/src/super_ai/alert_ingestion/routes.py`: bounded body reader, Bearer auth, safe responses.
- Create `apps/backend/src/super_ai/alert_ingestion/__init__.py`: package exports.
- Modify `apps/backend/src/super_ai/memory/models.py`: incident and event ORM models.
- Create `apps/backend/alembic/versions/202608220001_add_alert_ingestion.py`: PostgreSQL tables, checks, indexes, partial uniqueness.
- Modify `apps/backend/src/super_ai/api/app.py`: dependency assembly, router mount, metrics exposure.
- Modify `config/project.template.json` and `config/project.test.json`: disabled safe defaults; do not edit tracked secrets.
- Modify `infra/nginx/default.conf`, `infra/alertmanager/alertmanager.yml`, `infra/compose.yaml`, `.gitignore`: gateway/receiver/secret-file wiring.
- Modify `apps/backend/scripts/publish_ecommerce_quant_alert.py`: explicit firing/resolved publication.
- Create focused tests listed below; modify `apps/backend/tests/test_infra_compose.py` and `test_observability.py` only for contracts they already own.

---

### Task 1: Safe Alertmanager v4 Normalization

**Files:**
- Create: `apps/backend/src/super_ai/alert_ingestion/domain.py`
- Create: `apps/backend/src/super_ai/alert_ingestion/alertmanager.py`
- Create: `apps/backend/src/super_ai/alert_ingestion/__init__.py`
- Test: `apps/backend/tests/test_alertmanager_ingestion_parser.py`

**Interfaces:**
- Produces: `AlertPayloadError`, `NormalizedAlert`, `AlertmanagerDelivery`, and `parse_alertmanager_delivery(raw_body: bytes, *, max_alerts: int) -> AlertmanagerDelivery`.
- `AlertmanagerDelivery` exposes `status`, `group_key_hash`, `payload_sha256`, `alerts`, `normalized_payload`, `query`, and `truncated`.

- [ ] **Step 1: Write parser RED tests**

```python
def test_parser_keeps_only_allowlisted_fields_and_hashes_secrets() -> None:
    body = json.dumps({
        "version": "4", "status": "firing", "receiver": "agent-py",
        "groupKey": "secret-group", "ownerUserId": "attacker",
        "alerts": [{"status": "firing", "labels": {
            "alertname": "HighLatency", "service": "order-service", "unknown": "drop-me"
        }, "annotations": {"summary": "slow", "runbook_url": "drop-me"}}],
    }).encode()
    delivery = parse_alertmanager_delivery(body, max_alerts=50)
    serialized = json.dumps(delivery.normalized_payload)
    assert delivery.group_key_hash == hashlib.sha256(b"secret-group").hexdigest()
    assert delivery.payload_sha256 == hashlib.sha256(body).hexdigest()
    assert "secret-group" not in serialized
    assert "attacker" not in serialized
    assert "unknown" not in serialized
    assert "runbook_url" not in serialized

@pytest.mark.parametrize(
    ("field", "value"),
    [("version", "3"), ("status", "unknown"), ("groupKey", ""), ("alerts", [])],
)
def test_parser_rejects_invalid_required_fields(field: str, value: object) -> None:
    payload = valid_payload()
    payload[field] = value
    with pytest.raises(AlertPayloadError):
        parse_alertmanager_delivery(json.dumps(payload).encode(), max_alerts=50)
```

Also test empty alerts, 51 alerts, 2049-character annotation truncation, 257-character label truncation, unsafe `generatorURL`, origin-only external/generator URLs, stable query text, and ignored `executionPermitted`/knowledge-base fields.

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alertmanager_ingestion_parser.py -q`

Expected: collection fails because `super_ai.alert_ingestion` does not exist.

- [ ] **Step 3: Implement immutable domain and parser**

```python
class AlertPayloadError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class NormalizedAlert:
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: str | None
    ends_at: str | None
    generator_origin: str | None
    truncated: bool

@dataclass(frozen=True, slots=True)
class AlertmanagerDelivery:
    status: Literal["firing", "resolved"]
    receiver: str
    group_key_hash: str
    payload_sha256: str
    alerts: tuple[NormalizedAlert, ...]
    normalized_payload: dict[str, object]
    query: str
    truncated: bool

def parse_alertmanager_delivery(raw_body: bytes, *, max_alerts: int) -> AlertmanagerDelivery:
    payload_sha256 = sha256(raw_body).hexdigest()
    raw = _load_object(raw_body)
    _require_v4_contract(raw, max_alerts=max_alerts)
    alerts = tuple(_normalize_alert(value) for value in cast(list[object], raw["alerts"]))
    group_key = _required_string(raw, "groupKey")
    normalized = _normalized_delivery(raw, alerts)
    return AlertmanagerDelivery(
        status=cast(Literal["firing", "resolved"], raw["status"]),
        receiver=_bounded_string(raw.get("receiver"), 256),
        group_key_hash=sha256(group_key.encode()).hexdigest(),
        payload_sha256=payload_sha256,
        alerts=alerts,
        normalized_payload=normalized,
        query=_diagnostic_query(alerts[0]),
        truncated=any(alert.truncated for alert in alerts),
    )
```

Implement exact label allowlist `alertname, service, severity, environment, cluster, namespace, pod, instance, job, run_id, incident_id, trace_id`, annotation allowlist `summary, description, sop`, deterministic slicing, and `urlsplit` origin reconstruction.

- [ ] **Step 4: Verify GREEN and lint**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alertmanager_ingestion_parser.py -q && uv run --project apps/backend ruff check apps/backend/src/super_ai/alert_ingestion apps/backend/tests/test_alertmanager_ingestion_parser.py`

Expected: all parser tests pass and Ruff exits 0.

- [ ] **Step 5: Commit**

```text
git add apps/backend/src/super_ai/alert_ingestion apps/backend/tests/test_alertmanager_ingestion_parser.py
git commit -m "feat(alerts): normalize alertmanager webhook payloads"
```

### Task 2: Source-bound Configuration and Token Validation

**Files:**
- Create: `apps/backend/src/super_ai/alert_ingestion/config.py`
- Modify: `config/project.template.json`
- Modify: `config/project.test.json`
- Test: `apps/backend/tests/test_alert_ingestion_config.py`

**Interfaces:**
- Produces `AlertSourceConfig`, `AlertIngestionSettings`, and `load_alert_ingestion_settings(config_path, *, environ=None)`.
- `AlertSourceConfig.matches(labels: Mapping[str, str]) -> bool` requires every configured key to equal one allowed value on at least one alert.

- [ ] **Step 1: Write RED tests for disabled defaults and enabled validation**

```python
def test_enabled_source_binds_owner_kb_token_and_filters(tmp_path: Path) -> None:
    path = write_config(tmp_path, source(owner="svc", kb="kb_svc", token_env="HOOK_TOKEN"))
    settings = load_alert_ingestion_settings(path, environ={"HOOK_TOKEN": "x" * 32})
    assert settings.sources["local-alertmanager"].owner_user_id == "svc"
    assert settings.sources["local-alertmanager"].matches(
        {"environment": "test", "severity": "critical"}
    )

@pytest.mark.parametrize(
    "source_patch",
    [
        {"id": "../unsafe"},
        {"knowledgeBaseId": "kb_other"},
        {"allowedLabels": {}},
    ],
)
def test_invalid_enabled_source_fails_closed(
    source_patch: dict[str, object], tmp_path: Path
) -> None:
    configured = source(owner="svc", kb="kb_svc", token_env="HOOK_TOKEN")
    configured.update(source_patch)
    path = write_config(tmp_path, configured)
    with pytest.raises(ProjectConfigurationError):
        load_alert_ingestion_settings(path, environ={"HOOK_TOKEN": "x" * 32})

def test_short_token_fails_closed(tmp_path: Path) -> None:
    path = write_config(tmp_path, source(owner="svc", kb="kb_svc", token_env="HOOK_TOKEN"))
    with pytest.raises(ProjectConfigurationError):
        load_alert_ingestion_settings(path, environ={"HOOK_TOKEN": "short"})
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_config.py -q`

Expected: import failure for the missing config module.

- [ ] **Step 3: Implement strict configuration**

```python
@dataclass(frozen=True, slots=True)
class AlertSourceConfig:
    id: str
    owner_user_id: str
    knowledge_base_id: str
    token: str
    allowed_labels: dict[str, frozenset[str]]

@dataclass(frozen=True, slots=True)
class AlertIngestionSettings:
    enabled: bool
    max_body_bytes: int
    max_alerts_per_delivery: int
    redis_lease_milliseconds: int
    sources: dict[str, AlertSourceConfig]

def load_alert_ingestion_settings(config_path=None, *, environ=None) -> AlertIngestionSettings:
    section = load_project_config(config_path).get("alertIngestion")
    if section is None:
        return AlertIngestionSettings(False, 262144, 50, 2000, {})
    # Validate bounds, unique URL-safe IDs, owner/KB equality, non-empty filters,
    # environment variable names, and >=32-character token values for enabled sources.
```

Add an `alertIngestion` section with `enabled: false`, numeric limits, and no enabled sources to both tracked configs. Never add a token value.

- [ ] **Step 4: Verify GREEN**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_config.py apps/backend/tests/test_environment_examples.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```text
git add apps/backend/src/super_ai/alert_ingestion/config.py config/project.template.json config/project.test.json apps/backend/tests/test_alert_ingestion_config.py
git commit -m "feat(alerts): validate source-bound webhook configuration"
```

### Task 3: PostgreSQL Schema and Repository Contract

**Files:**
- Modify: `apps/backend/src/super_ai/memory/models.py`
- Create: `apps/backend/src/super_ai/alert_ingestion/repositories.py`
- Create: `apps/backend/alembic/versions/202608220001_add_alert_ingestion.py`
- Test: `apps/backend/tests/test_alert_ingestion_migration.py`

**Interfaces:**
- Produces `AlertIncidentModel`, `AlertEventModel`, `IncidentRecord`, `IngestionWrite`, `IngestionResult`, and `AlertIngestionRepository.apply(write: IngestionWrite) -> IngestionResult`.
- Dispositions are exactly `incident_created`, `duplicate_updated`, `incident_resolved`, `filtered`, and `orphan_resolved`.

- [ ] **Step 1: Write migration RED test**

```python
async def test_alert_schema_has_partial_active_uniqueness(migrated_database_url: str) -> None:
    engine = create_async_engine(migrated_database_url)
    async with engine.connect() as connection:
        indexes = (await connection.execute(text("""
          select indexdef from pg_indexes where tablename = 'aiops_alert_incidents'
        """))).scalars().all()
    assert any("owner_user_id" in value and "WHERE" in value and "active" in value for value in indexes)
```

Also assert both tables, status/disposition checks, 64-character hashes, unique event ID, owner-scoped indexes, and foreign keys to users/diagnostic tasks.

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_migration.py -q`

Expected: relation `aiops_alert_incidents` does not exist.

- [ ] **Step 3: Add models, protocol, and migration**

```python
class AlertIncidentModel(Base):
    __tablename__ = "aiops_alert_incidents"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    source_id: Mapped[str] = mapped_column(String(120), nullable=False)
    group_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    diagnostic_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("aiops_diagnostic_tasks.id", ondelete="SET NULL"), nullable=True
    )
    # alert_name/service/severity/timestamps/delivery_count follow the approved spec.
```

Create the partial unique index with:

```python
op.create_index(
    "uq_aiops_alert_incidents_active_group",
    "aiops_alert_incidents",
    ["owner_user_id", "source_id", "group_key_hash"],
    unique=True,
    postgresql_where=sa.text("status = 'active'"),
)
```

The event `normalized_payload` is JSONB and the incident stores only the three summary fields plus hashes/timestamps—not the raw normalized delivery.

- [ ] **Step 4: Verify migration round trip**

Run: `uv run --project apps/backend alembic -c apps/backend/alembic.ini upgrade head && uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_migration.py -q`

Expected: migration and schema tests pass.

- [ ] **Step 5: Commit**

```text
git add apps/backend/src/super_ai/memory/models.py apps/backend/src/super_ai/alert_ingestion/repositories.py apps/backend/alembic/versions/202608220001_add_alert_ingestion.py apps/backend/tests/test_alert_ingestion_migration.py
git commit -m "feat(alerts): add incident and event persistence schema"
```

### Task 4: Atomic PostgreSQL Incident State Machine

**Files:**
- Create: `apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py`
- Test: `apps/backend/tests/test_postgresql_alert_ingestion.py`

**Interfaces:**
- Consumes `IngestionWrite` and existing `DiagnosticTaskModel`, `BackgroundJobModel`.
- Produces `SQLAlchemyAlertIngestionRepository(session_factory).apply(write)` and owner-scoped `get_incident`/`list_events` test/read methods.

- [ ] **Step 1: Write RED transaction tests**

```python
@pytest.mark.asyncio
async def test_twenty_concurrent_firings_create_one_job(migrated_database_url: str) -> None:
    repository = build_repository(migrated_database_url)
    results = await asyncio.gather(*(repository.apply(firing(i)) for i in range(20)))
    assert sum(result.disposition == "incident_created" for result in results) == 1
    assert len({result.incident_id for result in results}) == 1
    assert await count_rows("aiops_diagnostic_tasks") == 1
    assert await count_rows("background_jobs") == 1
    assert await active_incident_delivery_count() == 20
```

Also test stable-event retry deduplication, resolved/orphan resolved, firing after resolved, commit-order race, owner isolation, and a successful follow-up operation on the same repository after a uniqueness conflict.

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_postgresql_alert_ingestion.py -q`

Expected: import failure for `SQLAlchemyAlertIngestionRepository`.

- [ ] **Step 3: Implement one-transaction state transitions**

```python
class SQLAlchemyAlertIngestionRepository:
    async def apply(self, write: IngestionWrite) -> IngestionResult:
        try:
            async with self._session_factory() as session, session.begin():
                active = await self._active_for_update(session, write)
                if write.filtered:
                    return await self._record_terminal_event(session, write, "filtered")
                if write.status == "resolved":
                    return await self._resolve_or_record_orphan(session, active, write)
                if active is not None:
                    return await self._update_duplicate(session, active, write)
                created = await self._insert_active_on_conflict_do_nothing(session, write)
                if created is None:
                    active = await self._active_for_update(session, write)
                    assert active is not None
                    return await self._update_duplicate(session, active, write)
                task_id, job_id = self._stable_work_ids(created.id)
                session.add(DiagnosticTaskModel(
                    id=task_id, owner_user_id=write.owner_user_id, status="accepted",
                    query=write.query,
                    input_payload={"query": write.query, "alert": write.safe_alert},
                    result_payload={}, created_at=write.received_at,
                    updated_at=write.received_at, completed_at=None,
                ))
                session.add(BackgroundJobModel(
                    id=job_id, owner_user_id=write.owner_user_id, kind="aiops_diagnosis",
                    resource_type="aiops_diagnostic", resource_id=task_id, status="queued",
                    payload={"diagnosticId": task_id}, attempt=0, max_attempts=3,
                    timeout_seconds=1800, available_at=write.received_at,
                    created_at=write.received_at, updated_at=write.received_at,
                ))
                created.diagnostic_task_id = task_id
                await self._insert_event_on_conflict_do_nothing(session, write, created.id)
                return self._result(created, task_id, job_id, "incident_created")
        except SQLAlchemyError as exc:
            raise AlertPersistenceError("alert persistence unavailable") from exc
```

Use PostgreSQL `insert(...).on_conflict_do_nothing().returning(...)`; never catch `IntegrityError` inside a poisoned transaction. The outer `SQLAlchemyError` boundary includes connection, flush, and `session.begin()` exit/commit failures and exposes only `AlertPersistenceError`. Add a real failing-commit/`OperationalError` test proving rollback, zero partial incident/task/job rows, a 503 at the API boundary, and no Runtime wakeup. Event ID is SHA-256 of owner/source/status/payload hash with an `alert_event_` prefix. Every authenticated delivery increments `delivery_count`, even when event insertion conflicts.

- [ ] **Step 4: Verify GREEN under real PostgreSQL concurrency**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_postgresql_alert_ingestion.py -q`

Expected: all state-machine and 20-way concurrency tests pass.

- [ ] **Step 5: Commit**

```text
git add apps/backend/src/super_ai/alert_ingestion/sqlalchemy.py apps/backend/tests/test_postgresql_alert_ingestion.py
git commit -m "feat(alerts): atomically enqueue incident diagnostics"
```

### Task 5: Redis Short Lease with Safe Degradation

**Files:**
- Create: `apps/backend/src/super_ai/alert_ingestion/redis_runtime.py`
- Test: `apps/backend/tests/test_alert_ingestion_redis.py`

**Interfaces:**
- Produces `AlertLease`, `AlertLeaseManager.acquire(source_id, group_key_hash) -> AlertLease` and `AlertLease.release() -> None`.
- Lease mode is `primary`, `contended`, or `degraded`; none of these modes can skip PostgreSQL.

- [ ] **Step 1: Write RED tests**

```python
async def test_lease_uses_set_nx_px_and_compare_delete() -> None:
    client = RecordingRedis(set_result=True)
    lease = await AlertLeaseManager(client, lease_ms=2000).acquire("source", "a" * 64)
    await lease.release()
    assert client.set_call.kwargs == {"nx": True, "px": 2000}
    assert "redis.call('get', KEYS[1])" in client.eval_script

async def test_redis_timeout_returns_degraded_lease() -> None:
    lease = await AlertLeaseManager(FailingRedis(), lease_ms=2000).acquire("source", "a" * 64)
    assert lease.mode == "degraded"
```

Also assert the key contains only source plus hash, contended acquisition waits no longer than the configured bounded delay, and release failure is swallowed/metricized.

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_redis.py -q`

Expected: missing module import.

- [ ] **Step 3: Implement lease adapter**

```python
_COMPARE_DELETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

async def acquire(self, source_id: str, group_key_hash: str) -> AlertLease:
    key = f"agentpy:alert-lease:{source_id}:{group_key_hash}"
    token = secrets.token_hex(16)
    try:
        acquired = await asyncio.wait_for(
            self._client.set(key, token, nx=True, px=self._lease_ms), timeout=0.25
        )
    except Exception:
        return AlertLease(mode="degraded", release_callback=_noop)
    if not acquired:
        await asyncio.sleep(min(self._lease_ms / 1000 / 10, 0.05))
        return AlertLease(mode="contended", release_callback=_noop)
    return AlertLease(mode="primary", release_callback=lambda: self._release(key, token))
```

- [ ] **Step 4: Verify GREEN**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_redis.py -q`

Expected: all lease tests pass.

- [ ] **Step 5: Commit**

```text
git add apps/backend/src/super_ai/alert_ingestion/redis_runtime.py apps/backend/tests/test_alert_ingestion_redis.py
git commit -m "feat(alerts): add optional redis ingestion lease"
```

### Task 6: Ingestion Service and Metrics

**Files:**
- Create: `apps/backend/src/super_ai/alert_ingestion/metrics.py`
- Create: `apps/backend/src/super_ai/alert_ingestion/service.py`
- Test: `apps/backend/tests/test_alert_ingestion_service.py`

**Interfaces:**
- Produces `AlertIngestionMetrics.snapshot()`, `AlertIngestionService.ingest(source, delivery) -> IngestionResult`.
- Service always releases an acquired lease and records one bounded disposition/latency sample.
- Metric semantics are exact: `webhookReceivedTotal` increments at route entry for every request, including 401/404; 401/404 do not increment `ingestionFailedTotal`; 413/422/503 increment `ingestionFailedTotal`; a committed disposition increments exactly its matching success counter; Redis fallback additionally increments `redisDegradedTotal`; a post-commit Runtime wake failure is logged but is not an ingestion failure.

- [ ] **Step 1: Write RED orchestration tests**

```python
async def test_filtered_delivery_is_audited_without_job() -> None:
    result = await service.ingest(source, nonmatching_delivery())
    assert result.disposition == "filtered"
    assert repository.writes[0].filtered is True
    assert metrics.snapshot()["filteredTotal"] == 1

async def test_redis_failure_still_calls_repository() -> None:
    result = await degraded_service.ingest(source, firing_delivery())
    assert result.redis_mode == "degraded"
    assert repository.apply_count == 1
    assert metrics.snapshot()["redisDegradedTotal"] == 1
```

Also test disposition counters, count/sum/max latency, enqueue latency only for `incident_created`, and safe structured event fields.

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_service.py -q`

Expected: missing service/metrics modules.

- [ ] **Step 3: Implement orchestration and bounded metrics**

```python
async def ingest(self, source: AlertSourceConfig, delivery: AlertmanagerDelivery) -> IngestionResult:
    started = monotonic()
    filtered = not any(source.matches(alert.labels) for alert in delivery.alerts)
    lease = await self._leases.acquire(source.id, delivery.group_key_hash)
    try:
        result = await self._repository.apply(
            IngestionWrite.from_delivery(source=source, delivery=delivery, filtered=filtered)
        )
    except Exception:
        self._metrics.record_failure(elapsed_ms(started))
        raise
    finally:
        await lease.release()
    self._metrics.record_success(result.disposition, lease.mode, elapsed_ms(started))
    return replace(result, redis_mode=lease.mode)
```

Use a `threading.Lock` like existing observability primitives. Define `record_received()`, `record_request_failure()`, `record_success(...)`, and `record_redis_degraded()` so the route and service share one metrics instance. Metrics expose exact camelCase keys from the spec and no labels derived from payload data. Add table-driven assertions for 401, 404, 413, 422, 503, Redis degradation, every committed disposition, and post-commit Runtime wake failure.

- [ ] **Step 4: Verify GREEN**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_service.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```text
git add apps/backend/src/super_ai/alert_ingestion/metrics.py apps/backend/src/super_ai/alert_ingestion/service.py apps/backend/tests/test_alert_ingestion_service.py
git commit -m "feat(alerts): orchestrate incident ingestion safely"
```

### Task 7: FastAPI Webhook and Safe Failure Semantics

**Files:**
- Create: `apps/backend/src/super_ai/alert_ingestion/routes.py`
- Test: `apps/backend/tests/test_alert_ingestion_api.py`

**Interfaces:**
- Produces `create_alert_ingestion_router(settings, service, runtime, metrics) -> APIRouter`.
- Success response keys are exactly `status`, `incidentId`, `diagnosticTaskId`, `duplicate`, `filtered`, and `redisMode`.

- [ ] **Step 1: Write API RED tests**

```python
@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer wrong"])
async def test_invalid_authorization_is_401_without_persistence(
    authorization: str | None, webhook_client: WebhookClient
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await webhook_client.post(valid_body(), headers=headers)
    assert response.status_code == 401
    assert webhook_client.repository.apply_count == 0

async def test_actual_stream_over_limit_is_413_even_without_content_length(
    webhook_client: WebhookClient,
) -> None:
    response = await webhook_client.post_stream([b"x" * 200_000, b"y" * 70_000])
    assert response.status_code == 413
    assert webhook_client.repository.apply_count == 0

async def test_database_failure_is_retryable_503(failing_repository_client: WebhookClient) -> None:
    response = await failing_repository_client.post(valid_body())
    assert response.status_code == 503

async def test_runtime_wakeup_failure_remains_202_after_commit(
    failing_runtime_client: WebhookClient,
) -> None:
    response = await failing_runtime_client.post(valid_body())
    assert response.status_code == 202
    assert failing_runtime_client.repository.apply_count == 1
```

Cover 202 for first/duplicate/filtered/resolved/orphan, 404 for unknown/disabled source, 413 declared and actual byte limits, 422 JSON/schema errors, `hmac.compare_digest`, no repository call before auth/validation, and response/log scans for token, raw group key, unknown fields, and raw annotation text.

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_api.py -q`

Expected: missing routes module.

- [ ] **Step 3: Implement bounded request reader and router**

```python
async def _read_bounded_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdecimal() or int(declared) > limit):
        raise HTTPException(status_code=413)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413)
    return bytes(body)

@router.post("/{source_id}", status_code=202)
async def receive(source_id: str, request: Request) -> JSONResponse:
    metrics.record_received()
    source = settings.sources.get(source_id)
    if source is None:
        raise HTTPException(status_code=404)
    _authenticate(request.headers.get("authorization"), source.token)
    raw = await _read_bounded_body(request, settings.max_body_bytes)
    try:
        delivery = parse_alertmanager_delivery(raw, max_alerts=settings.max_alerts_per_delivery)
        result = await service.ingest(source, delivery)
    except AlertPayloadError as exc:
        metrics.record_request_failure()
        raise HTTPException(status_code=422) from exc
    except AlertPersistenceError as exc:
        metrics.record_request_failure()
        raise HTTPException(status_code=503) from exc
    if result.disposition == "incident_created":
        try:
            await runtime.start()
        except Exception:
            logger.warning("alert.ingestion.worker_wakeup_failed", extra={"sourceId": source.id})
    return JSONResponse(status_code=202, content=result.safe_response())
```

The bounded body reader calls `record_request_failure()` before each 413. Authenticate before parsing. Ensure errors never interpolate exception messages that may contain database or payload values. Runtime wake failure remains a successful committed ingestion and therefore does not increment `ingestionFailedTotal`.

- [ ] **Step 4: Verify GREEN**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_api.py -q`

Expected: all status/safety tests pass.

- [ ] **Step 5: Commit**

```text
git add apps/backend/src/super_ai/alert_ingestion/routes.py apps/backend/tests/test_alert_ingestion_api.py
git commit -m "feat(alerts): expose authenticated alertmanager webhook"
```

### Task 8: Application Assembly and Metrics Exposure

**Files:**
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/tests/test_observability.py`
- Test: `apps/backend/tests/test_alert_ingestion_app.py`

**Interfaces:**
- `create_app` accepts optional `alert_ingestion_service` and `alert_lease_manager` test seams; defaults compose SQLAlchemy plus the app Redis client.
- `/metrics` adds `alertIngestion` without changing existing keys.

- [ ] **Step 1: Write assembly RED tests**

```python
def test_create_app_mounts_webhook_only_when_ingestion_enabled(tmp_path: Path) -> None:
    disabled = create_app(project_config_path=write_ingestion_config(tmp_path, enabled=False))
    enabled = create_app(
        project_config_path=write_ingestion_config(tmp_path, enabled=True),
        alert_ingestion_service=FakeAlertIngestionService(),
    )
    disabled_paths = {route.path for route in disabled.routes}
    enabled_paths = {route.path for route in enabled.routes}
    path = "/aiops/alerts/webhook/alertmanager/{source_id}"
    assert path not in disabled_paths
    assert path in enabled_paths

async def test_metrics_include_alert_ingestion_snapshot(app_client) -> None:
    payload = (await app_client.get("/metrics")).json()["data"]
    assert payload["alertIngestion"]["webhookReceivedTotal"] == 0
```

Also assert an enabled invalid source fails during `create_app`, disabled defaults do not require a token, existing `aiops_diagnosis` handler remains registered, and automatic job payload contains no `executionPermitted`.

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_app.py apps/backend/tests/test_observability.py -q`

Expected: metrics key/router are absent.

- [ ] **Step 3: Compose dependencies and include router**

```python
settings = load_alert_ingestion_settings(resolved_project_config_path)
alert_metrics = AlertIngestionMetrics()
alert_repository = SQLAlchemyAlertIngestionRepository(session_factory)
lease_manager = AlertLeaseManager(composed_redis_client, lease_ms=settings.redis_lease_milliseconds)
service = alert_ingestion_service or AlertIngestionService(
    alert_repository, lease_manager, alert_metrics
)
app.state.alert_ingestion_metrics = alert_metrics
if settings.enabled:
    app.include_router(create_alert_ingestion_router(settings, service, background_runtime))
```

Place composition after the Redis client exists, or split Redis composition into a helper so construction order is explicit. Extend `/metrics` with `request.app.state.alert_ingestion_metrics.snapshot()`.

- [ ] **Step 4: Verify focused existing API behavior**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_app.py apps/backend/tests/test_observability.py apps/backend/tests/test_aiops_diagnostics.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```text
git add apps/backend/src/super_ai/api/app.py apps/backend/tests/test_alert_ingestion_app.py apps/backend/tests/test_observability.py
git commit -m "feat(alerts): assemble ingestion runtime and metrics"
```

### Task 9: Nginx and Alertmanager Secret-file Wiring

**Files:**
- Modify: `infra/nginx/default.conf`
- Modify: `infra/alertmanager/alertmanager.yml`
- Modify: `infra/compose.yaml`
- Modify: `.gitignore`
- Modify: `apps/backend/tests/test_infra_compose.py`
- Create: `apps/backend/tests/test_alert_ingestion_infra.py`

**Interfaces:**
- Nginx path exactly matches the API; receiver uses Compose DNS `nginx` and a mounted credentials file.
- Developer secret path is `infra/secrets/alert_webhook_token`, ignored by Git.

- [ ] **Step 1: Write infra RED tests**

```python
def test_webhook_has_independent_gateway_budget() -> None:
    nginx = read("infra/nginx/default.conf")
    assert "zone=alert_webhook_per_ip:10m rate=5r/s" in nginx
    assert "location ~ ^/aiops/alerts/webhook/alertmanager/" in nginx
    assert "limit_req zone=alert_webhook_per_ip burst=20 nodelay" in nginx
    assert "client_max_body_size 256k" in nginx

def test_alertmanager_uses_bearer_secret_file() -> None:
    config = read("infra/alertmanager/alertmanager.yml")
    assert "group_by: [alertname, service, environment, run_id]" in config
    assert "http://nginx/aiops/alerts/webhook/alertmanager/local-alertmanager" in config
    assert "credentials_file: /run/secrets/alert_webhook_token" in config
```

Also assert `send_resolved: true`, `max_alerts: 50`, secret read-only mount, Nginx dependency, no literal token, and ignored local secret file.

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_infra.py apps/backend/tests/test_infra_compose.py -q`

Expected: receiver and dedicated rate-limit assertions fail.

- [ ] **Step 3: Apply gateway and receiver configuration**

```nginx
limit_req_zone $binary_remote_addr zone=alert_webhook_per_ip:10m rate=5r/s;

location ~ ^/aiops/alerts/webhook/alertmanager/[A-Za-z0-9._-]+$ {
    limit_req zone=alert_webhook_per_ip burst=20 nodelay;
    client_max_body_size 256k;
    include /etc/nginx/includes/proxy-common.conf;
    proxy_pass http://agent_py_backend;
}
```

```yaml
route:
  receiver: agent-py-webhook
  group_by: [alertname, service, environment, run_id]

receivers:
  - name: agent-py-webhook
    webhook_configs:
      - url: http://nginx/aiops/alerts/webhook/alertmanager/local-alertmanager
        send_resolved: true
        max_alerts: 50
        http_config:
          authorization:
            type: Bearer
            credentials_file: /run/secrets/alert_webhook_token
```

Mount `./secrets/alert_webhook_token:/run/secrets/alert_webhook_token:ro`, add `nginx` dependency, and ignore `infra/secrets/*` while keeping an optional tracked `.gitkeep` exception only if the directory must exist.

- [ ] **Step 4: Verify configs and tests**

Run: `docker compose -f infra/compose.yaml config -q && uv run --project apps/backend pytest apps/backend/tests/test_alert_ingestion_infra.py apps/backend/tests/test_infra_compose.py -q`

Expected: Compose renders and all infrastructure contract tests pass.

- [ ] **Step 5: Commit**

```text
git add infra/nginx/default.conf infra/alertmanager/alertmanager.yml infra/compose.yaml .gitignore apps/backend/tests/test_alert_ingestion_infra.py apps/backend/tests/test_infra_compose.py
git commit -m "feat(alerts): route alertmanager webhooks through nginx"
```

### Task 10: Firing and Resolved Publisher Support

**Files:**
- Modify: `apps/backend/scripts/publish_ecommerce_quant_alert.py`
- Test: `apps/backend/tests/test_publish_ecommerce_alerts.py`

**Interfaces:**
- CLI accepts `--status firing|resolved`, `--alertmanager-url`, and `--group-key`; defaults remain safe for local development.
- Publisher sends Alertmanager API v2 alert objects; Alertmanager generates the Webhook v4 delivery.

- [ ] **Step 1: Write publisher RED tests**

```python
def test_resolved_publication_sets_ends_at_and_preserves_labels(monkeypatch) -> None:
    request = capture_publish(["--status", "resolved", "--group-key", "demo"])
    assert request.alerts[0]["endsAt"] is not None
    assert request.alerts[0]["labels"]["run_id"] == "demo"
```

Also test firing has no past `endsAt`, invalid status fails argument parsing, firing and resolved preserve the identical `alertname/service/environment/run_id` grouping labels, changing `--group-key` changes only `run_id`, and logs omit Authorization/secret values. The CLI name `--group-key` is a developer-facing lifecycle handle; it does not directly set Alertmanager's Webhook `groupKey`. Alertmanager derives the actual top-level `groupKey` from the configured `group_by` labels.

- [ ] **Step 2: Verify RED**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_publish_ecommerce_alerts.py -q`

Expected: CLI does not accept the new arguments.

- [ ] **Step 3: Add deterministic CLI arguments**

```python
parser.add_argument("--status", choices=("firing", "resolved"), default="firing")
parser.add_argument("--group-key", default=f"agentpy-{uuid4().hex[:12]}")
ends_at = now if args.status == "resolved" else None
labels["run_id"] = args.group_key
```

Do not add the Webhook token to this publisher; it talks to Alertmanager, which owns the receiver secret.

- [ ] **Step 4: Verify GREEN**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_publish_ecommerce_alerts.py -q`

Expected: all publisher tests pass.

- [ ] **Step 5: Commit**

```text
git add apps/backend/scripts/publish_ecommerce_quant_alert.py apps/backend/tests/test_publish_ecommerce_alerts.py
git commit -m "feat(alerts): publish firing and resolved test alerts"
```

### Task 11: Focused Offline Security and Regression Gate

**Files:**
- Modify only files identified by failures in the commands below; do not broaden scope.

**Interfaces:**
- Produces a clean focused pytest/Ruff/Pyright result and migration head.

- [ ] **Step 1: Run all alert-ingestion tests together**

Run: `uv run --project apps/backend pytest apps/backend/tests/test_alertmanager_ingestion_parser.py apps/backend/tests/test_alert_ingestion_config.py apps/backend/tests/test_alert_ingestion_migration.py apps/backend/tests/test_postgresql_alert_ingestion.py apps/backend/tests/test_alert_ingestion_redis.py apps/backend/tests/test_alert_ingestion_service.py apps/backend/tests/test_alert_ingestion_api.py apps/backend/tests/test_alert_ingestion_app.py apps/backend/tests/test_alert_ingestion_infra.py apps/backend/tests/test_publish_ecommerce_alerts.py apps/backend/tests/test_observability.py apps/backend/tests/test_infra_compose.py apps/backend/tests/test_aiops_diagnostics.py -q`

Expected: all selected tests pass with no skips introduced by this feature.

- [ ] **Step 2: Run sensitive-data source scan**

Run: `rg -n "AGENTPY_ALERT_WEBHOOK_TOKEN_LOCAL\s*[:=]\s*[^\"']|Bearer [A-Za-z0-9_-]{16,}|secret-group|drop-me" apps/backend/src apps/backend/tests infra config --glob '!*.pyc'`

Expected: only intentional test fixtures/assertions appear; no tracked token value, raw group key logging, or production payload fixture appears.

- [ ] **Step 3: Run Ruff and Pyright on touched Python paths**

Run: `uv run --project apps/backend ruff check apps/backend/src/super_ai/alert_ingestion apps/backend/src/super_ai/api/app.py apps/backend/src/super_ai/memory/models.py apps/backend/tests/test_alert* apps/backend/tests/test_postgresql_alert_ingestion.py apps/backend/tests/test_publish_ecommerce_alerts.py`

Run: `uv run --project apps/backend pyright apps/backend/src/super_ai/alert_ingestion apps/backend/src/super_ai/api/app.py apps/backend/src/super_ai/memory/models.py`

Expected: both commands exit 0.

- [ ] **Step 4: Verify migration head**

Run: `uv run --project apps/backend alembic -c apps/backend/alembic.ini heads`

Expected: exactly `202608220001 (head)`.

- [ ] **Step 5: Commit any narrowly required corrections**

```text
git add apps/backend/src/super_ai/alert_ingestion apps/backend/src/super_ai/api/app.py apps/backend/src/super_ai/memory/models.py apps/backend/tests/test_alert* apps/backend/tests/test_postgresql_alert_ingestion.py apps/backend/tests/test_publish_ecommerce_alerts.py
git commit -m "test(alerts): harden ingestion regression coverage"
```

Skip this commit when the worktree is already clean.

### Task 12: Local Alertmanager-to-LangGraph Acceptance

**Files:**
- No tracked production edits expected.
- Runtime-only secret: `infra/secrets/alert_webhook_token` (ignored).
- Runtime-only source/token override: existing ignored `config/user.project.json`; preserve all unrelated user values.

**Interfaces:**
- Verifies the actual chain Alertmanager → Nginx → webhook → PostgreSQL → BackgroundJobRuntime → existing `aiops_diagnosis` → persisted report.

- [ ] **Step 1: Create a local 32+ character token without printing it**

Run: PowerShell creates `infra/secrets/alert_webhook_token` with restricted local contents and sets `AGENTPY_ALERT_WEBHOOK_TOKEN_LOCAL` in the backend process environment. Do not echo, log, commit, or paste its value.

Expected: the secret file exists locally and `git status --short` does not list it.

- [ ] **Step 2: Enable only the local source in ignored user configuration**

Use:

```json
{
  "alertIngestion": {
    "enabled": true,
    "sources": [{
      "id": "local-alertmanager",
      "enabled": true,
      "ownerUserId": "user_service_account",
      "knowledgeBaseId": "kb_user_service_account",
      "tokenEnvironmentVariable": "AGENTPY_ALERT_WEBHOOK_TOKEN_LOCAL",
      "allowedLabels": {"environment": ["test"], "severity": ["warning", "critical"]}
    }]
  }
}
```

Expected: application config check succeeds; the service account and its knowledge base already exist.

- [ ] **Step 3: Start local dependencies and backend**

Run: `docker compose -f infra/compose.yaml up -d postgres redis nginx alertmanager`

Run backend with the existing documented `uvicorn super_ai.api.app:create_app` command and token environment variable in the same process.

Expected: PostgreSQL/Redis/Nginx/Alertmanager become healthy and backend `/ready` has no blocking failure.

- [ ] **Step 4: Publish a unique firing lifecycle**

Run: `uv run --project apps/backend python apps/backend/scripts/publish_ecommerce_quant_alert.py --status firing --group-key alert-ingestion-acceptance-<timestamp>`

Expected within 2 seconds of delivery: one active incident, one accepted/running diagnostic task, and one queued/running background job. Poll existing diagnostic APIs/database safely until one persisted report exists; do not impose the 2-second requirement on Agent completion.

- [ ] **Step 5: Prove duplicate suppression**

Run the same firing command again with the identical group key.

Expected: incident `delivery_count` increases; diagnostic-task/job counts remain exactly one; `duplicateSuppressedTotal` increases.

- [ ] **Step 6: Prove resolved behavior**

Run: `uv run --project apps/backend python apps/backend/scripts/publish_ecommerce_quant_alert.py --status resolved --group-key alert-ingestion-acceptance-<same-timestamp>`

Expected: incident becomes resolved and the existing diagnosis is not cancelled. Attribute model usage by `diagnostic_task_id`/background-job ID: resolved creates no new diagnostic task/job and triggers no calls under a new diagnostic ID; calls from the already-running original diagnosis may continue naturally.

- [ ] **Step 7: Prove a new lifecycle after resolution**

Publish firing once more with the same group key.

Expected: a second incident lifecycle and exactly one new diagnostic job are created.

- [ ] **Step 8: Prove Redis degradation correctness**

Stop only Redis, repeat the same firing delivery concurrently, then restart Redis.

Expected: Webhook returns 202, `redisMode`/metrics report degraded operation, and PostgreSQL still permits only one active incident/job for that lifecycle.

- [ ] **Step 9: Inspect safety and clean runtime test rows**

Query only the acceptance lifecycle rows and scan stored JSON/logs for the test token, raw `groupKey`, unknown annotations, `ownerUserId`, `knowledgeBaseId`, and `executionPermitted`.

Expected: zero sensitive matches; reports and job audit remain available until explicitly deleting only rows whose IDs/group hashes were recorded for this acceptance. Cleanup must target exact acceptance IDs and must not truncate shared tables.

- [ ] **Step 10: Record final evidence and commit only documentation if needed**

Record command timestamps, HTTP statuses, incident/task/job IDs, dispositions, report ID, duplicate counts, Redis mode, and focused test hashes in the existing project verification log if such a tracked log already exists. Do not create a new reporting subsystem.

Expected: worktree contains only intended source/config/test/docs changes and no secrets or runtime artifacts.

## Final Acceptance Checklist

- [ ] Legal firing reaches the webhook through Nginx and receives 202 within 2 seconds.
- [ ] Twenty concurrent same-group firing deliveries create exactly one active incident, task, and job.
- [ ] Duplicate firing, filtered delivery, resolved delivery, and orphan resolved produce the correct disposition without an unnecessary Agent call.
- [ ] Resolved does not cancel an in-flight diagnosis; firing after resolved creates one new lifecycle.
- [ ] Redis unavailable still yields PostgreSQL-correct 202 behavior; PostgreSQL failure yields 503.
- [ ] Worker wake failure after commit yields 202 and leaves a claimable durable job.
- [ ] Stored JSON, responses, and logs contain no token, raw body, raw group key, unknown fields, or payload-provided authority.
- [ ] Automatic task input cannot grant recovery execution and still reaches the existing Policy Gate.
- [ ] Alert ingestion metrics expose all approved counters and latency count/sum/max values.
- [ ] Focused pytest, Ruff, Pyright, Compose rendering, and Alembic head checks pass.
- [ ] One real local firing lifecycle produces a persisted LangGraph diagnostic report.
