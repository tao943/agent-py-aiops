# Knowledge Index Polling and Bulk Import Design

Date: 2026-08-12

## Goal

Make document-index polling reliable and reusable, then use the same tested path to import a reviewed batch of troubleshooting knowledge into the existing RAG knowledge base.

The observed empty polling status is not evidence of an intermittent backend failure. The temporary PowerShell importer read `data.task.status`, while the task-query endpoint returns the task directly as `data`, so the correct path is `data.status`.

## Scope

This change will:

- preserve the current backend API and storage architecture;
- add a reusable Python client-side polling helper;
- make the existing SOP seed script use that helper;
- add a dedicated batch importer for reviewed Markdown knowledge cards;
- add focused polling, API-contract, and batch-import tests;
- import only explicitly selected files from `docs/knowledge-candidates`;
- verify completed imports through the public API and, during the final integration check, PostgreSQL/Milvus evidence.

This change will not:

- add a new backend bulk-job endpoint;
- poll PostgreSQL directly from an importer;
- change the indexing worker or task schema;
- copy full third-party blog posts;
- put Benchmark ground truth into the RAG knowledge base;
- delete the two earlier duplicate documents without separate approval.

## Reuse Assessment

The project already has the required building blocks: `httpx`, document upload and index-task APIs, project configuration loading, authentication logic, and `_wait_for_index` in `seed_ecommerce_aiops_sop.py`. No new dependency or external service is required.

The selected approach is wrapped internal adoption: extract and harden the existing polling behavior behind a small project-owned helper, then reuse it from both seed commands. Generic polling packages or a new task-queue dependency would add integration and supply-chain cost without solving a missing capability. External implementations are useful only as reference for bounded retries and deadlines; the local API contract remains authoritative.

## API Contract

The importer must treat creation and query responses as distinct contracts:

- `POST .../index-tasks` returns `data.task` plus scheduling metadata.
- `GET .../index-tasks/{task_id}` returns the task payload directly in `data`.

An API regression test will assert the query response shape and status field explicitly. Client parsing will fail with a clear protocol error when `data` is not an object or `status` is missing, empty, or not a string. It will not silently treat malformed responses as a still-running task.

## Components

### Polling helper

A small synchronous helper under `apps/backend/scripts` will own the task-query loop. Its inputs are an `httpx.Client`, endpoint, headers, polling interval, deadline, and bounded transient-retry settings. Its only success result is a terminal `succeeded` state.

State handling:

- `pending` and `running`: wait and query again;
- `succeeded`: return the parsed task;
- `failed` and `cancelled`: raise a terminal indexing error containing the reported failure reason when present;
- unknown status: raise a protocol error;
- elapsed deadline: raise an explicit timeout error.

Transport timeouts and transient connection failures are retried with bounded backoff while the overall deadline remains authoritative. HTTP status errors and malformed successful responses are surfaced rather than hidden. Sleep and monotonic-clock functions will be injectable for deterministic unit tests, without adding production-only test hooks.

### Existing SOP seed command

`seed_ecommerce_aiops_sop.py` will retain its current CLI and upload behavior. Its local polling loop will be replaced by the shared helper. Authentication and task-creation parsing remain local unless a small extraction clearly removes duplication during refactoring.

### Batch knowledge importer

A dedicated Python command will accept an explicit source directory or file list, discover Markdown files deterministically, and import them sequentially through the existing API. Sequential execution is intentional for the first version: it respects embedding quota, makes failures attributable, and avoids duplicate concurrent work.

For each file, the command will:

1. upload with the existing overwrite behavior;
2. create an index task;
3. poll that task through the shared helper;
4. record filename, document ID, task ID, and final status;
5. continue or stop according to an explicit CLI failure policy, defaulting to fail-fast.

The command will print a final machine-readable summary that contains counts and identifiers but never credentials or document bodies. Re-running relies on the existing upload overwrite/idempotency semantics rather than inventing a second persistence layer.

## Data and Safety Boundaries

Only reviewed, original troubleshooting summaries with source attribution are eligible. The importer will ignore non-Markdown files, reject paths outside the selected source directory, and use configuration without printing the API key or access token.

RAG content contains diagnostic knowledge, evidence-gathering guidance, and remediation considerations. Benchmark oracle fields such as primary cause, required answer, hidden evidence, or scoring ground truth remain outside imported documents.

## Failure Handling

- A transient query transport failure consumes a bounded retry and remains subject to the global deadline.
- A terminal indexing failure is reported with file and task context.
- A malformed API response fails immediately as a contract violation.
- A deadline expiry reports the last known status when available.
- A partial batch produces an accurate summary of succeeded and failed files; it never labels an unconfirmed item as indexed.
- Import verification must not rely on `/ready`, because the currently unavailable CLS MCP integration is unrelated to document indexing.

## Testing Strategy

Development follows red-green-refactor.

Focused unit tests will cover:

- `pending -> running -> succeeded`;
- transient timeout followed by success;
- terminal `failed` and `cancelled` states;
- deadline expiry;
- malformed or missing `data.status`;
- unknown status;
- deterministic Markdown discovery and path containment;
- partial-batch reporting and fail-fast behavior.

The existing indexing API test will explicitly assert `GET` returns `data.status`, not `data.task.status`. Script-level tests will use `httpx.MockTransport` or an equivalent existing HTTPX mechanism, avoiding live services for ordinary CI.

Final verification will run focused Pytest tests, Ruff, and Pyright. A live import will then process the reviewed knowledge batch against the local backend. Success requires all selected files to report `succeeded`, with PostgreSQL task/document records and Milvus indexing evidence checked afterward. Live import is not part of ordinary CI because it requires the local service stack and embedding API quota.

## Acceptance Criteria

- No polling code reads query status from `data.task.status`.
- The shared poller has deterministic tests for success, transient failure, terminal failure, timeout, and malformed responses.
- Both seed workflows use the same poller.
- The batch command imports exactly the selected reviewed Markdown files and produces an auditable summary.
- Ordinary tests do not require DashScope, PostgreSQL, Milvus, Redis, or CLS.
- The final live verification confirms every newly selected document is indexed without exposing secrets.
