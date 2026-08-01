# Fix Test Environment and Live LLM Layers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Do not dispatch subagents for this change. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backend suite reproducible on Windows/Python 3.13, restore the twelve failing offline tests, and add opt-in real DashScope smoke tests.

**Architecture:** Keep all default tests offline by constructing explicit fake-backed configuration and excluding a registered `live_llm` marker. Stabilize the development environment through uv dependency overrides and a repository-local pytest temporary directory, then repair cross-platform and stale test fixtures without weakening production behavior.

**Tech Stack:** Python 3.10+, uv, pytest/pytest-asyncio, FastAPI, Redis, LangChain OpenAI-compatible Qwen, DashScope, PostgreSQL

## Global Constraints

- Do not dispatch subagents.
- Never print, log, diff, or commit the DashScope API key.
- Keep `config/project.json` and `config/user.project.json` ignored and untracked.
- Default `uv run pytest` must not call DashScope.
- Only `pytest -m live_llm` may load local LLM credentials and make real requests.
- Keep PostgreSQL as the only relational runtime and preserve Redis rate-limit semantics.

---

### Task 1: Stabilize uv and pytest on Windows/Python 3.13

**Files:**
- Modify: `apps/backend/pyproject.toml`
- Modify: `apps/backend/uv.lock`
- Modify: `apps/backend/tests/test_environment_examples.py`

**Interfaces:**
- Consumes: `tencentcloud-cls-sdk-python==1.0.4`, which requests `python-snappy` transitively.
- Produces: a lock graph using `python-snappy>=0.7.3` and default pytest options using `var/pytest` while excluding `live_llm`.

- [ ] **Step 1: Add configuration assertions before changing pyproject**

Add a test that reads `apps/backend/pyproject.toml` and asserts:

```python
assert '"python-snappy>=0.7.3"' in pyproject
assert "--basetemp=var/pytest" in pyproject
assert "not live_llm" in pyproject
assert 'live_llm = "calls the configured real DashScope models"' in pyproject
```

- [ ] **Step 2: Verify the configuration test fails**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_environment_examples.py::test_backend_test_runtime_is_reproducible -q --basetemp=var/pytest-red-runtime
```

Expected: FAIL because the override, marker, and local basetemp are absent.

- [ ] **Step 3: Implement the uv and pytest configuration**

Set:

```toml
[tool.uv]
override-dependencies = ["protobuf>=5.27.2", "python-snappy>=0.7.3"]

[tool.pytest.ini_options]
addopts = "-q --basetemp=var/pytest -m 'not live_llm'"
asyncio_mode = "auto"
pythonpath = ["src"]
testpaths = ["tests"]
markers = ["live_llm: calls the configured real DashScope models"]
```

Regenerate only the affected lock entry:

```powershell
uv lock --upgrade-package python-snappy
```

- [ ] **Step 4: Verify dependency and pytest configuration**

Run:

```powershell
uv sync --frozen
.venv\Scripts\python.exe -m pytest tests/test_environment_examples.py::test_backend_test_runtime_is_reproducible -q
```

Expected: uv installs a wheel-backed `python-snappy>=0.7.3`; the test passes without system-temp errors.

- [ ] **Step 5: Commit**

```powershell
git add -- apps/backend/pyproject.toml apps/backend/uv.lock apps/backend/tests/test_environment_examples.py
git commit -m "fix: stabilize backend test runtime"
```

### Task 2: Normalize uploaded Skill newlines and Bash validation

**Files:**
- Modify: `apps/backend/src/super_ai/chat/configuration.py`
- Modify: `apps/backend/tests/test_skill_examples.py`
- Modify: `apps/backend/tests/test_local_development_docs.py`

**Interfaces:**
- Consumes: UTF-8 `SKILL.md` bytes containing LF, CRLF, or CR line endings.
- Produces: `ValidatedChatSkill.content` with LF-only newlines and a platform-aware Bash syntax test.

- [ ] **Step 1: Add a focused CRLF normalization test**

Add:

```python
def test_skill_upload_normalizes_windows_newlines() -> None:
    content = b"---\r\nname: ops\r\ndescription: Ops\r\n---\r\n# Ops\r\n"

    validated = validate_skill_upload("SKILL.md", content)

    assert validated.content == "---\nname: ops\ndescription: Ops\n---\n# Ops"
```

- [ ] **Step 2: Verify RED**

Run the new test and expect a CRLF-versus-LF assertion failure.

- [ ] **Step 3: Normalize newlines in production validation**

After UTF-8 decoding, use:

```python
normalized_content = decoded.replace("\r\n", "\n").replace("\r", "\n").strip()
```

Update the repository example test to assert LF-only content.

- [ ] **Step 4: Make Bash discovery platform-aware**

In `test_local_development_docs.py`, resolve Bash as follows:

```python
def _bash_command() -> str | None:
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if git_bash.exists():
            return str(git_bash)
    return shutil.which("bash")
```

Skip when no usable executable exists and pass the script through standard input:

```python
result = subprocess.run(
    [bash, "-n"],
    input=shell_launcher.read_text(encoding="utf-8"),
    check=False,
    capture_output=True,
    text=True,
)
```

- [ ] **Step 5: Verify and commit**

Run both affected files, then commit:

```powershell
git add -- apps/backend/src/super_ai/chat/configuration.py apps/backend/tests/test_skill_examples.py apps/backend/tests/test_local_development_docs.py
git commit -m "fix: normalize cross-platform test inputs"
```

### Task 3: Repair Redis and readiness test doubles

**Files:**
- Modify: `apps/backend/tests/test_aiops_diagnostics.py`
- Modify: `apps/backend/tests/test_readiness_api.py`

**Interfaces:**
- Consumes: `RedisRateLimitClient.eval(...) -> Awaitable[object]` and `RateLimitService.acquire(...) -> RateLimitDecision`.
- Produces: failure doubles that exercise Redis fallback and readiness tests isolated from rate-limit configuration.

- [ ] **Step 1: Use the existing failures as RED evidence**

Run the three failing nodes and confirm `AttributeError: eval` plus missing `rateLimits` configuration.

- [ ] **Step 2: Complete the unavailable Redis double**

Implement:

```python
async def eval(self, _script: str, _numkeys: int, *_keys_and_args: object) -> object:
    raise RedisError("Redis is unavailable")
```

- [ ] **Step 3: Add an allow-all rate-limit double**

In `test_readiness_api.py`, add:

```python
class AllowAllRateLimitService:
    async def acquire(self, *, owner_id: str, action: str) -> RateLimitDecision:
        del owner_id, action
        return RateLimitDecision(True, 1, 0, "local_fallback")
```

Pass `rate_limit_service=AllowAllRateLimitService()` to the two `/config/check` applications that intentionally use partial or invalid project JSON.

- [ ] **Step 4: Verify GREEN and commit**

Run the three nodes, expect PASS, then commit both test files with `test: refresh Redis and readiness doubles`.

### Task 4: Align templates, PostgreSQL-only docs, and resilient documentation tests

**Files:**
- Modify: `config/project.template.json`
- Modify: `infra/README.md`
- Modify: `apps/backend/tests/test_environment_examples.py`
- Modify: `apps/backend/tests/test_database_config.py`
- Modify: `apps/backend/tests/test_infra_compose.py`

**Interfaces:**
- Consumes: committed sanitized templates and current PostgreSQL/Redis Compose topology.
- Produces: tests that validate committed configuration rather than local credentials or line wrapping.

- [ ] **Step 1: Preserve the current failures as RED evidence**

Run the three affected test files and confirm failures for MinIO defaults, external Prometheus placeholder, local LLM values, SQLite wording, and wrapped infrastructure prose.

- [ ] **Step 2: Sanitize the committed MinIO template**

Set:

```json
"minio": {"accessKey": "", "secretKey": ""}
```

- [ ] **Step 3: Replace local-config assertions with template merge assertions**

Use `tmp_path`, copy `project.template.json`, write a fake `user.project.json` with `offline-test-key`, load through `load_project_config`, and assert the merged provider fields without reading the repository's ignored local files. Assert the external Prometheus URL is an empty placeholder and the local Alertmanager URL remains `http://127.0.0.1:9093/api/v2/alerts`.

- [ ] **Step 4: Remove legacy migration wording and brittle prose assertions**

Delete the SQLite migration sentence from `infra/README.md`. In `test_infra_compose.py`, assert each component token (`etcd`, `MinIO`, `Milvus`, `Attu`, `Alertmanager`) and the local-process statement independently rather than one wrapped line.

- [ ] **Step 5: Verify and commit**

Run the three files, expect PASS, then commit with `fix: align environment examples with local config policy`.

### Task 5: Isolate offline LLM tests and add opt-in live smoke tests

**Files:**
- Modify: `apps/backend/tests/test_llm_provider.py`
- Create: `apps/backend/tests/test_live_llm.py`
- Modify: `apps/backend/README.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: `load_llm_provider_config`, `QwenOpenAIProvider`, `EmbeddingModel.aembed_documents`, and `RerankModel.arerank`.
- Produces: local-config-independent offline tests and explicit live tests selected by `-m live_llm`.

- [ ] **Step 1: Convert local-dependent tests to explicit offline config**

Change the two tests that call `load_llm_provider_config()` to accept `tmp_path`, create config with `_write_config(..., api_key="offline-test-key", chat_model="qwen-test-chat")`, and pass the path explicitly. Assertions must use the fake model name and must not require an `sk-` prefix.

- [ ] **Step 2: Verify offline LLM tests pass with an invalid local Key**

Run `tests/test_llm_provider.py`; expected PASS because all provider calls use Fake models and explicit fake configuration.

- [ ] **Step 3: Add live provider fixture and smoke tests**

Create a module marked with:

```python
pytestmark = pytest.mark.live_llm
```

The module fixture catches `LlmConfigurationError` and calls `pytest.skip`. Add async tests that:

```python
readiness = await provider.check_readiness()
assert readiness.ok, readiness.error

vectors = await provider.create_embedding_model().aembed_documents(["Agent runtime readiness"])
assert len(vectors) == 1
assert len(vectors[0]) == provider.config.embedding_dimensions
assert all(math.isfinite(value) for value in vectors[0])

rankings = await provider.create_rerank_model().arerank(
    query="How do I recover an unavailable API?",
    documents=["Restart the API after checking health probes.", "A recipe for noodles."],
    top_n=2,
)
assert {item.index for item in rankings} == {0, 1}
```

- [ ] **Step 4: Document exact offline and live commands**

Document:

```powershell
uv run pytest
uv run pytest -m live_llm tests/test_live_llm.py -q
```

State that only the second command reads local credentials and consumes quota.

- [ ] **Step 5: Verify marker isolation and live calls**

Run `pytest --collect-only` and confirm live tests are deselected by default. Then explicitly run the three live tests once; ensure failures are secret-safe.

- [ ] **Step 6: Commit**

Commit the four files with `test: separate offline and live LLM coverage`.

### Task 6: Full verification and handoff

**Files:**
- Verify all files changed by Tasks 1-5.

**Interfaces:**
- Consumes: the stabilized dependency graph and test layers.
- Produces: evidence that the backend is ready for branch completion without credential leakage.

- [ ] **Step 1: Run focused regression tests**

Run all previously failing files and expect zero failures.

- [ ] **Step 2: Run quality gates**

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\pyright.exe
```

Expected: zero Ruff and Pyright errors.

- [ ] **Step 3: Run the complete offline suite**

```powershell
.venv\Scripts\python.exe -m pytest
```

Expected: all offline tests pass and all `live_llm` tests are deselected.

- [ ] **Step 4: Validate repository state**

Run `git diff --check`, `git status --short`, and `git check-ignore` for both local config files. Do not display local JSON contents.

- [ ] **Step 5: Commit any final documentation-only corrections**

If verification requires documentation-only corrections, commit only those exact paths with `docs: finalize backend test workflow`. Otherwise create no extra commit.
