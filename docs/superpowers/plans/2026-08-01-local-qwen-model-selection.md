# Local Qwen Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Do not dispatch subagents for this change.

**Goal:** Secure local DashScope credentials and configure quota-backed Qwen models without exposing the API key.

**Architecture:** Keep committed templates as the public configuration contract while local runtime JSON files remain untracked and ignored. Override only local LLM model fields, preserving the existing API key and the current 1024-dimensional Milvus contract.

**Tech Stack:** Git, JSON project configuration, Python 3.10+, pytest

## Global Constraints

- Never print, log, diff, or commit the DashScope API key.
- Keep `config/project.template.json`, `config/user.project.template.json`, and `config/project.test.json` tracked.
- Use `qwen3.7-plus`, `qwen3.7-text-embedding`, 1024 dimensions, and `qwen3-vl-rerank`.
- Do not run real provider calls during offline configuration verification.

---

### Task 1: Protect local configuration files

**Files:**
- Modify: `.gitignore`
- Untrack while preserving locally: `config/project.json`
- Untrack while preserving locally: `config/user.project.json`

**Interfaces:**
- Consumes: Git index entries for the two local configuration files.
- Produces: ignored local configuration paths with no credential-bearing Git diff.

- [x] **Step 1: Add exact ignore rules**

Add these lines to `.gitignore`:

```gitignore
config/project.json
config/user.project.json
```

- [x] **Step 2: Stop tracking the files without deleting local copies**

Run:

```powershell
git rm --cached -- config/project.json config/user.project.json
```

Expected: both paths are staged as deletions while both files still exist in the working tree.

- [x] **Step 3: Verify ignore and preservation**

Run `git check-ignore -v config/project.json config/user.project.json` and `Test-Path` for both files.

Expected: both paths match `.gitignore`, and both `Test-Path` results are `True`.

### Task 2: Apply and validate local Qwen selection

**Files:**
- Modify locally only: `config/user.project.json`

**Interfaces:**
- Consumes: existing non-empty `llm.apiKey` without reading it into output.
- Produces: merged `LlmProviderConfig` with the approved models and dimensions.

- [x] **Step 1: Update non-secret LLM fields**

Preserve the existing `apiKey` and set:

```json
{
  "chatModel": "qwen3.7-plus",
  "embeddingModel": "qwen3.7-text-embedding",
  "embeddingDimensions": 1024,
  "rerankModel": "qwen3-vl-rerank",
  "modelCapabilities": {
    "qwen3.7-plus": {
      "contextWindowTokens": 1000000
    }
  }
}
```

- [x] **Step 2: Validate configuration without exposing the key**

Load `LlmProviderConfig` with the worktree virtual environment and emit only model names, dimensions, context-window size, and a boolean indicating that the key is present.

Expected: approved model values, `1024`, `1000000`, and `api_key_present=True`.

- [x] **Step 3: Run offline configuration tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_llm_provider.py tests/test_environment_examples.py -q
```

Expected: tests complete without making real provider requests. Any assertions tied to obsolete tracked local defaults must be reported separately rather than weakening secret safety.

### Task 3: Commit only repository-safe changes

**Files:**
- Commit: `.gitignore`
- Commit staged removal: `config/project.json`
- Commit staged removal: `config/user.project.json`
- Commit: `docs/superpowers/plans/2026-08-01-local-qwen-model-selection.md`

**Interfaces:**
- Consumes: verified safe Git index.
- Produces: repository state where local configuration cannot be accidentally re-added.

- [x] **Step 1: Audit the staged paths**

Run `git diff --cached --name-status` and confirm only the four paths above are staged. Do not display the deleted files' contents.

- [x] **Step 2: Check and commit**

Run `git diff --cached --check`, then commit with:

```powershell
git commit -m "fix: keep local credentials out of git"
```

- [x] **Step 3: Final safety check**

Run `git status --short`, `git check-ignore` for both local JSON files, and a redacted configuration load.

Expected: clean worktree, both local files ignored, and approved models load with a non-empty key.
