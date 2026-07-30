# PostgreSQL-only Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every SQLite runtime and integration-test path with PostgreSQL 16 while preserving the durable background-job runtime and making task claiming/event sequencing safe across multiple backend workers.

**Architecture:** SQLAlchemy async repositories remain behind the existing repository protocols, but use `asyncpg`, PostgreSQL transactions, JSONB, and row-level locks. PostgreSQL is the only relational source of truth. Alembic creates a fresh development database; no SQLite data importer is added because the supplied source contains no SQLite database to preserve.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.x async, asyncpg, Alembic, PostgreSQL 16, pytest, Docker Compose.

## Global Constraints

- Work in a real Git clone. The supplied source extract has no `.git`; clone or restore repository metadata before executing commit steps.
- Do not add MySQL, SQLite compatibility, dual-write logic, or a data migration utility.
- Do not replace `BackgroundJobRuntime` with Celery, RQ, or a Redis queue.
- Preserve existing repository protocols and API response shapes unless a task explicitly changes them.
- Run tests against a real PostgreSQL service. Do not mock SQLAlchemy sessions for integration behavior.
- Complete tasks in order. Each task ends with a focused commit.

---

## Task 1: Add PostgreSQL development and test infrastructure

**Files:**

- Modify: `infra/compose.yaml`
- Create: `infra/postgres/init/001-create-test-database.sql`
- Modify: `infra/README.md`
- Modify: `config/project.json`
- Modify: `config/project.template.json`
- Create: `config/project.test.json`
- Modify: `apps/backend/pyproject.toml`
- Test: `apps/backend/tests/test_database_config.py`

- [ ] **Step 1: Write failing configuration tests**

Create `apps/backend/tests/test_database_config.py`:

```python
from pathlib import Path

from super_ai.memory.database import load_memory_database_settings


ROOT = Path(__file__).resolve().parents[3]


def test_development_config_uses_postgresql_asyncpg() -> None:
    settings = load_memory_database_settings(ROOT / "config" / "project.json")
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert "sqlite" not in settings.database_url


def test_test_config_targets_isolated_database() -> None:
    settings = load_memory_database_settings(ROOT / "config" / "project.test.json")
    assert settings.database_url.endswith("/agent_py_test")
```

- [ ] **Step 2: Run the test and confirm it fails**

Run:

```powershell
Set-Location apps/backend
uv run pytest tests/test_database_config.py -q
```

Expected: failure because current configuration still uses `sqlite+aiosqlite`.

- [ ] **Step 3: Replace dependencies and configuration**

In `apps/backend/pyproject.toml`, remove `aiosqlite` and add:

```toml
"asyncpg>=0.30.0",
```

Remove `backend.memoryDatabaseUrl` and set `backend.databaseUrl` in `config/project.json` and `config/project.template.json` to:

```json
"postgresql+asyncpg://agent_py:agent_py_dev@localhost:5432/agent_py"
```

Create `config/project.test.json` by copying the full project structure and changing only `backend.databaseUrl` to:

```json
"postgresql+asyncpg://agent_py:agent_py_dev@localhost:5432/agent_py_test"
```

- [ ] **Step 4: Add PostgreSQL 16 to Compose**

Add a `postgres` service with a named `postgres-data` volume, health check using `pg_isready`, port `5432`, database `agent_py`, user `agent_py`, and development password `agent_py_dev`. Mount `./postgres/init:/docker-entrypoint-initdb.d:ro`.

Create `infra/postgres/init/001-create-test-database.sql`:

```sql
CREATE DATABASE agent_py_test OWNER agent_py;
```

Document startup, health inspection, development credentials, and the fact that init scripts only run on a new volume.

- [ ] **Step 5: Sync dependencies and start PostgreSQL**

Run:

```powershell
Set-Location apps/backend
uv sync
Set-Location ../../infra
docker compose up -d postgres
docker compose ps postgres
```

Expected: PostgreSQL reports `healthy`.

- [ ] **Step 6: Re-run the configuration tests**

Run:

```powershell
Set-Location ../apps/backend
uv run pytest tests/test_database_config.py -q
```

Expected: `2 passed`.

- [ ] **Step 7: Commit**

```powershell
git add -- infra/compose.yaml infra/postgres/init/001-create-test-database.sql infra/README.md config/project.json config/project.template.json config/project.test.json apps/backend/pyproject.toml apps/backend/uv.lock apps/backend/tests/test_database_config.py
git commit -m "build: add PostgreSQL development runtime"
```

---

## Task 2: Make database construction PostgreSQL-specific

**Files:**

- Modify: `apps/backend/src/super_ai/memory/database.py`
- Modify: `apps/backend/src/super_ai/memory/__init__.py`
- Modify: `apps/backend/alembic/env.py`
- Test: `apps/backend/tests/test_database_config.py`

- [ ] **Step 1: Add failing engine tests**

Append:

```python
import pytest

from super_ai.memory.database import create_memory_engine


def test_engine_rejects_non_postgresql_urls() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        create_memory_engine("sqlite+aiosqlite:///memory.sqlite3")


def test_engine_uses_asyncpg_driver() -> None:
    engine = create_memory_engine(
        "postgresql+asyncpg://agent_py:agent_py_dev@localhost:5432/agent_py_test"
    )
    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "asyncpg"
```

- [ ] **Step 2: Confirm the rejection test fails**

Run:

```powershell
uv run pytest tests/test_database_config.py -q
```

Expected: `test_engine_rejects_non_postgresql_urls` fails.

- [ ] **Step 3: Enforce the database contract**

Keep `MemoryDatabaseSettings`, `load_memory_database_settings`, `create_memory_engine`, and `create_memory_session_factory` as public names so Alembic and app composition remain stable. Make the loader require `backend.databaseUrl`, change the default URL to PostgreSQL, and reject URLs whose SQLAlchemy backend/driver is not `postgresql+asyncpg`.

Engine construction must use:

```python
create_async_engine(
    url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
```

Do not pass SQLite-only `connect_args`.

- [ ] **Step 4: Make Alembic fail fast on wrong configuration**

In `apps/backend/alembic/env.py`, load `config/project.json` as before and let `create_memory_engine` enforce PostgreSQL. Preserve both offline and online migration entry points.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
uv run pytest tests/test_database_config.py -q
```

Expected: `4 passed`.

- [ ] **Step 6: Commit**

```powershell
git add -- apps/backend/src/super_ai/memory/database.py apps/backend/src/super_ai/memory/__init__.py apps/backend/alembic/env.py apps/backend/tests/test_database_config.py
git commit -m "refactor: enforce PostgreSQL-only database settings"
```

---

## Task 3: Establish a real PostgreSQL integration-test harness

**Files:**

- Create: `apps/backend/tests/conftest.py`
- Create: `apps/backend/tests/test_postgresql_migrations.py`
- Modify: `apps/backend/tests/test_active_alerts.py`
- Modify: `apps/backend/tests/test_aiops_diagnostics.py`
- Modify: `apps/backend/tests/test_auth_api.py`
- Modify: `apps/backend/tests/test_auth_migrations.py`
- Modify: `apps/backend/tests/test_auth_service.py`
- Modify: `apps/backend/tests/test_chat_memory.py`
- Modify: `apps/backend/tests/test_chat_sessions_api.py`
- Modify: `apps/backend/tests/test_document_indexing.py`
- Modify: `apps/backend/tests/test_document_indexing_api.py`
- Modify: `apps/backend/tests/test_extended_capabilities.py`
- Modify: `apps/backend/tests/test_knowledge_documents_api.py`
- Modify: `apps/backend/tests/test_knowledge_retrieval_api.py`
- Modify: `apps/backend/tests/test_memory_migrations.py`
- Modify: `apps/backend/tests/test_memory_repositories.py`
- Modify: `apps/backend/tests/test_milvus_vector_store.py`
- Modify: `apps/backend/tests/test_readiness_api.py`
- Modify: `apps/backend/tests/test_stream_rag_chat_api.py`
- Modify: `apps/backend/tests/test_tool_call_audits.py`

- [ ] **Step 1: Write a migration smoke test**

Create `apps/backend/tests/test_postgresql_migrations.py`:

```python
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def test_alembic_head_exists_in_postgresql(migrated_database_url: str) -> None:
    engine = create_async_engine(migrated_database_url)
    async with engine.connect() as connection:
        dialect = await connection.scalar(text("select current_setting('server_version_num')"))
        revision = await connection.scalar(text("select version_num from alembic_version"))
    await engine.dispose()
    assert int(dialect) >= 160000
    assert revision
```

- [ ] **Step 2: Create the shared fixture**

In `apps/backend/tests/conftest.py`:

- Resolve `config/project.test.json`.
- Build an `alembic.config.Config` from `apps/backend/alembic.ini`, call `set_main_option("sqlalchemy.url", test_settings.database_url)`, and invoke `alembic.command.upgrade(config, "head")` once per test session. Update `alembic/env.py` to prefer Alembic’s explicit `sqlalchemy.url` when present and otherwise load `backend.databaseUrl`.
- Before every test, truncate every table in schema `public` except `alembic_version` using quoted identifiers and one generated `TRUNCATE` statement with `RESTART IDENTITY CASCADE`.
- Return the PostgreSQL URL from the existing `migrated_database_url` fixture name.
- Fail with an actionable message if PostgreSQL is unreachable.

The cleanup query must discover targets from `pg_tables`; it must never drop the database or schema.

- [ ] **Step 3: Remove duplicated SQLite fixtures**

Delete each file-local `migrated_database_url(tmp_path)` implementation from the listed tests. Keep all call sites unchanged so they receive the shared PostgreSQL fixture.

- [ ] **Step 4: Run migration and repository smoke tests**

Run:

```powershell
uv run pytest tests/test_postgresql_migrations.py tests/test_memory_repositories.py -q
```

Expected: all tests pass against `agent_py_test`; no `.sqlite3` file is created.

- [ ] **Step 5: Prove the suite contains no SQLite test URL**

Run:

```powershell
rg "sqlite|aiosqlite" tests src pyproject.toml
```

Expected: no runtime or integration-test matches. Historical migration comments may be reworded rather than retained.

- [ ] **Step 6: Commit**

```powershell
git add -- apps/backend/tests
git commit -m "test: run backend integration suite on PostgreSQL"
```

---

## Task 4: Convert relational models to PostgreSQL JSONB

**Files:**

- Modify: `apps/backend/src/super_ai/memory/models.py`
- Create: `apps/backend/alembic/versions/202607300001_postgresql_jsonb.py`
- Test: `apps/backend/tests/test_postgresql_migrations.py`

- [ ] **Step 1: Add schema assertions**

Append a parametrized test that queries `information_schema.columns` and asserts `data_type == "jsonb"` for:

```text
background_jobs.payload
background_job_events.payload
mcp_connections.last_tools
knowledge_documents.metadata
user_chat_configurations.skill_ids
chat_messages.metadata
tool_call_audits.arguments
aiops_diagnostic_tasks.input_payload
aiops_diagnostic_tasks.result_payload
aiops_diagnostic_reports.payload
aiops_diagnostic_cases.keywords
aiops_diagnostic_cases.evidence_ids
aiops_diagnostic_steps.payload
aiops_diagnostic_evidence.payload
aiops_tool_call_audits.arguments
aiops_tool_call_audits.result_payload
aiops_graph_checkpoints.checkpoint_payload
aiops_graph_checkpoints.metadata
```

- [ ] **Step 2: Confirm schema assertions fail**

Run:

```powershell
uv run pytest tests/test_postgresql_migrations.py -q
```

Expected: JSON columns report `json`, not `jsonb`.

- [ ] **Step 3: Use JSONB in the ORM and add a forward migration**

Import `JSONB` from `sqlalchemy.dialects.postgresql` and replace generic `JSON` for the listed columns.

Create revision `202607300001` with `down_revision` equal to the current head. For each listed column, use:

```python
op.alter_column(
    table_name,
    column_name,
    type_=postgresql.JSONB(astext_type=sa.Text()),
    postgresql_using=f"{column_name}::jsonb",
)
```

The downgrade converts each column to `sa.JSON()` with `postgresql_using=f"{column_name}::json"`.

- [ ] **Step 4: Rebuild the test database and run assertions**

Because this is a fresh-development migration, recreate only the `agent_py_test` database through the documented test setup, then run:

```powershell
uv run alembic upgrade head
uv run pytest tests/test_postgresql_migrations.py -q
```

Expected: all migration assertions pass.

- [ ] **Step 5: Commit**

```powershell
git add -- apps/backend/src/super_ai/memory/models.py apps/backend/alembic/versions/202607300001_postgresql_jsonb.py apps/backend/tests/test_postgresql_migrations.py
git commit -m "refactor: use PostgreSQL JSONB models"
```

---

## Task 5: Rename SQLite repositories without changing their contracts

**Files:**

- Move: `apps/backend/src/super_ai/memory/sqlite.py` → `apps/backend/src/super_ai/memory/sqlalchemy.py`
- Move: `apps/backend/src/super_ai/memory/extended_sqlite.py` → `apps/backend/src/super_ai/memory/extended_sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/memory/__init__.py`
- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: all backend tests importing `SQLite*`

- [ ] **Step 1: Change tests to the intended public names**

Use `SQLAlchemyMemoryRepository`, `SQLAlchemyBackgroundJobRepository`, and `create_sqlalchemy_memory_repositories`. Preserve the existing constructor arguments and protocol types.

- [ ] **Step 2: Run focused tests and confirm import failures**

Run:

```powershell
uv run pytest tests/test_memory_repositories.py tests/test_extended_capabilities.py -q
```

Expected: collection fails because the new public names do not exist.

- [ ] **Step 3: Move modules and rename implementations**

Perform a semantic rename:

- every class whose name starts with `SQLite` → the same suffix prefixed with `SQLAlchemy`
- `create_sqlite_memory_repositories` → `create_sqlalchemy_memory_repositories`
- SQLite-specific docstrings → PostgreSQL/SQLAlchemy descriptions

Do not change protocol methods, records, or response serialization in this task.

- [ ] **Step 4: Update application composition**

In `apps/backend/src/super_ai/api/app.py`, construct repositories through `create_sqlalchemy_memory_repositories`. Update exports in `memory/__init__.py`. Verify:

```powershell
rg "SQLite|sqlite|extended_sqlite" src tests
```

Expected: no matches.

- [ ] **Step 5: Run focused repository tests**

Run:

```powershell
uv run pytest tests/test_memory_repositories.py tests/test_extended_capabilities.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add -- apps/backend/src/super_ai/memory apps/backend/src/super_ai/api/app.py apps/backend/tests
git commit -m "refactor: generalize memory repositories for PostgreSQL"
```

---

## Task 6: Make background-job claiming and event sequencing multi-worker safe

**Files:**

- Modify: `apps/backend/src/super_ai/memory/extended_sqlalchemy.py`
- Modify: `apps/backend/src/super_ai/jobs/runtime.py`
- Create: `apps/backend/tests/test_postgresql_background_jobs.py`
- Modify: `apps/backend/tests/test_extended_capabilities.py`

- [ ] **Step 1: Write concurrent-claim tests**

Create two queued jobs, then call `claim_next` concurrently from two independent repository/session instances using `asyncio.gather`. Assert:

- both calls return a job;
- returned IDs are distinct;
- both rows are `running`;
- a third claim returns `None`.

Add a lease-expiry test proving an expired running job becomes claimable exactly once.

- [ ] **Step 2: Write concurrent event-sequence tests**

Append 20 events concurrently to one job through independent sessions. Assert stored sequences equal `1..20`, contain no duplicates, and are ordered by sequence.

- [ ] **Step 3: Confirm the concurrency tests fail**

Run:

```powershell
uv run pytest tests/test_postgresql_background_jobs.py -q
```

Expected: duplicate claims or duplicate event sequences expose the current select-then-update implementation.

- [ ] **Step 4: Implement atomic claiming**

Within one transaction:

1. Reset expired leases.
2. Select the oldest eligible queued job using:

```python
select(BackgroundJobModel)
.where(BackgroundJobModel.status == "queued")
.order_by(BackgroundJobModel.created_at, BackgroundJobModel.id)
.with_for_update(skip_locked=True)
.limit(1)
```

3. Set status, owner, lease expiry, and attempt count.
4. Flush and return the record before committing.

Do not hold a transaction while a handler performs external work.

- [ ] **Step 5: Serialize event numbering per job**

At the beginning of `append_event`, lock the parent `BackgroundJobModel` row with `FOR UPDATE`, then calculate `max(sequence) + 1`, insert the event, flush, and commit. Raise the existing not-found error if the parent job does not exist.

- [ ] **Step 6: Update runtime terminology**

Remove “SQLite-leased” wording from `jobs/runtime.py`. Preserve polling interval, lease renewal, retry, timeout, cancellation, and handler registration behavior.

- [ ] **Step 7: Run concurrency and runtime tests**

Run:

```powershell
uv run pytest tests/test_postgresql_background_jobs.py tests/test_extended_capabilities.py -q
```

Expected: all pass repeatedly. Run the command three times to catch race regressions.

- [ ] **Step 8: Commit**

```powershell
git add -- apps/backend/src/super_ai/memory/extended_sqlalchemy.py apps/backend/src/super_ai/jobs/runtime.py apps/backend/tests/test_postgresql_background_jobs.py apps/backend/tests/test_extended_capabilities.py
git commit -m "fix: make PostgreSQL job leasing multi-worker safe"
```

---

## Task 7: Replace SQLite readiness with PostgreSQL readiness

**Files:**

- Modify: `apps/backend/src/super_ai/api/app.py`
- Modify: `apps/backend/tests/test_readiness_api.py`

- [ ] **Step 1: Update contract tests first**

Change expected dependency key from `sqlite` to `postgresql` and assert:

```json
{
  "engine": "postgresql",
  "driver": "asyncpg",
  "ok": true
}
```

- [ ] **Step 2: Confirm readiness tests fail**

Run the focused backend readiness test file identified by:

```powershell
rg -l '"sqlite"|_sqlite_readiness_payload' apps/backend/tests
```

Expected: current response still returns `sqlite`.

- [ ] **Step 3: Implement PostgreSQL readiness**

Rename `_sqlite_readiness_payload` to `_postgresql_readiness_payload`. Execute `SELECT 1`, inspect `engine.dialect.name` and `engine.dialect.driver`, and return an actionable error without credentials on failure.

Change `_runtime_dependency_payload` to expose `postgresql`. Do not change readiness semantics for Milvus, LLM, or MCP in this plan.

- [ ] **Step 4: Run backend tests**

Run:

```powershell
Set-Location apps/backend
uv run pytest -q
```

Expected: all tests pass and no runtime code references SQLite.

- [ ] **Step 5: Commit**

```powershell
git add -- apps/backend/src/super_ai/api/app.py apps/backend/tests/test_readiness_api.py
git commit -m "feat: report PostgreSQL runtime readiness"
```

---

## Task 8: Final PostgreSQL-only verification and documentation

**Files:**

- Modify: `README.md`
- Modify: `apps/backend/README.md`
- Modify: `openspec/changes/migrate-postgresql-add-redis-runtime/tasks.md`

- [ ] **Step 1: Document the operational flow**

Document:

- PostgreSQL 16 startup and migration commands;
- the `agent_py` and `agent_py_test` databases;
- job lease and `SKIP LOCKED` behavior;
- fresh-database policy and absence of SQLite migration;
- how to inspect jobs/events in PostgreSQL.

- [ ] **Step 2: Run static checks**

Run:

```powershell
rg "sqlite|aiosqlite|SQLite" apps/backend config infra README.md
uv run ruff check src tests
uv run mypy src
```

Expected: no SQLite references; lint and type checks pass.

- [ ] **Step 3: Run full tests and migration validation**

Run:

```powershell
uv run alembic downgrade base
uv run alembic upgrade head
uv run pytest -q
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate migrate-postgresql-add-redis-runtime --strict
```

Expected: downgrade/upgrade succeeds on `agent_py_test`, tests pass, and OpenSpec reports the change is valid.

- [ ] **Step 4: Update OpenSpec task state**

Check only the PostgreSQL-related boxes whose commands passed in `openspec/changes/migrate-postgresql-add-redis-runtime/tasks.md`. Leave Redis-related boxes unchecked.

- [ ] **Step 5: Commit**

```powershell
git add -- README.md apps/backend/README.md openspec/changes/migrate-postgresql-add-redis-runtime/tasks.md
git commit -m "docs: complete PostgreSQL-only runtime migration"
```
