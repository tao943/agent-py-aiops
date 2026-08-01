# GitHub Actions 第一版 CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Agent Py 增加路径感知、无真实凭据、带 PostgreSQL/Redis 集成测试和稳定 `CI Gate` 的第一版 GitHub Actions CI。

**Architecture:** 一个始终触发的 workflow 先用仓库内 Python 脚本计算 backend、frontend、docs/spec 三类变更，再条件执行并行检查。另一个脚本从安全模板生成 CI 临时本地配置；所有真实模型测试保持排除，最终 Gate 汇总实际执行结果。

**Tech Stack:** GitHub Actions、Python 3.13、uv 0.11.32、Ruff、Pyright、pytest、PostgreSQL 16、Redis 7、Node.js 22、npm、Vitest、Vite、OpenSpec 1.6.0、VitePress。

## Global Constraints

- 第一版只实现 CI，不实现部署、镜像发布或 CD。
- 普通 CI 不读取任何 GitHub secret，不运行 `live_llm`，不调用 DashScope、CLS、MCP、Milvus、Prometheus 或 Alertmanager。
- workflow 使用 `pull_request`，不得使用 `pull_request_target`，顶层权限为 `contents: read`。
- `CI Gate` 是名称稳定的唯一推荐 required check，接受 `success`/`skipped`，拒绝 `failure`/`cancelled`。
- Python 固定 3.13；Node.js 固定 22；uv 固定 0.11.32；OpenSpec 固定 1.6.0。
- PostgreSQL service 使用 `agent_py`/`agent_py_dev`，同时创建 `agent_py` 和 `agent_py_test`；Redis 测试使用 `/0` 与 `/15` 的现有契约。
- CI 配置只从 `config/*.template.json` 生成，固定假密钥为 `offline-test-key`，不得覆盖开发者已有本地配置。
- 不引入 pytest-xdist、覆盖率阈值、安全扫描、Python 矩阵或额外包管理器。
- 所有任务在当前会话内联执行，不启动多 Agent。

---

### Task 1: 确定性变更检测脚本

**Files:**
- Create: `scripts/ci/detect_changes.py`
- Create: `apps/backend/tests/test_ci_tooling.py`

**Interfaces:**
- Consumes: `--event <event>`、可选 `--base <sha>`、`--head <sha>`、可选 `--paths-file <path>`、`--output <path>`。
- Produces: GitHub output 格式的 `backend=true|false`、`frontend=true|false`、`docs_spec=true|false`。
- Behavior: 手动触发、无效 SHA、Git diff 失败、未知路径均返回全量 true；已知路径按设计分类。

- [ ] **Step 1: 写变更分类的失败测试**

在 `apps/backend/tests/test_ci_tooling.py` 中加入 subprocess 测试辅助函数和四个用例：

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DETECT_CHANGES = REPO_ROOT / "scripts" / "ci" / "detect_changes.py"


def _run_change_detection(tmp_path: Path, paths: list[str]) -> dict[str, str]:
    paths_file = tmp_path / "paths.txt"
    paths_file.write_text("\n".join(paths), encoding="utf-8")
    output_file = tmp_path / "github-output.txt"
    subprocess.run(
        [
            sys.executable,
            str(DETECT_CHANGES),
            "--event",
            "pull_request",
            "--paths-file",
            str(paths_file),
            "--output",
            str(output_file),
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    return dict(
        line.split("=", maxsplit=1)
        for line in output_file.read_text(encoding="utf-8").splitlines()
    )


def test_ci_change_detection_classifies_known_areas(tmp_path: Path) -> None:
    assert _run_change_detection(tmp_path, ["apps/backend/src/super_ai/api/app.py"]) == {
        "backend": "true",
        "frontend": "false",
        "docs_spec": "false",
    }
    assert _run_change_detection(tmp_path, ["apps/frontend/src/App.vue"]) == {
        "backend": "false",
        "frontend": "true",
        "docs_spec": "false",
    }
    assert _run_change_detection(tmp_path, ["docs/index.md"]) == {
        "backend": "false",
        "frontend": "false",
        "docs_spec": "true",
    }


def test_ci_change_detection_expands_shared_and_unknown_changes(tmp_path: Path) -> None:
    all_areas = {"backend": "true", "frontend": "true", "docs_spec": "true"}
    assert _run_change_detection(tmp_path, [".github/workflows/ci.yml"]) == all_areas
    assert _run_change_detection(tmp_path, ["unclassified-root-file.txt"]) == all_areas


def test_ci_change_detection_combines_multiple_areas(tmp_path: Path) -> None:
    assert _run_change_detection(
        tmp_path,
        ["apps/backend/pyproject.toml", "packages/api-contracts/src/index.ts"],
    ) == {"backend": "true", "frontend": "true", "docs_spec": "false"}


def test_ci_manual_dispatch_runs_every_area(tmp_path: Path) -> None:
    output_file = tmp_path / "github-output.txt"
    subprocess.run(
        [
            sys.executable,
            str(DETECT_CHANGES),
            "--event",
            "workflow_dispatch",
            "--output",
            str(output_file),
        ],
        cwd=REPO_ROOT,
        check=True,
    )
    assert output_file.read_text(encoding="utf-8").splitlines() == [
        "backend=true",
        "frontend=true",
        "docs_spec=true",
    ]
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_ci_tooling.py -q
```

Expected: FAIL，因为 `scripts/ci/detect_changes.py` 不存在。

- [ ] **Step 3: 实现最小变更检测脚本**

创建 `scripts/ci/detect_changes.py`：

```python
from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ZERO_SHA = "0" * 40
GLOBAL_PREFIXES = (".github/", "scripts/ci/")
GLOBAL_FILES = {"AGENTS.md"}
BACKEND_PREFIXES = ("apps/backend/", "config/", "infra/")
FRONTEND_PREFIXES = ("apps/frontend/", "packages/api-contracts/")
FRONTEND_FILES = {"package.json", "package-lock.json"}
DOCS_PREFIXES = ("docs/", "openspec/")
DOCS_FILES = {"README.md", "openspec从0到1项目实战的提示词.md"}


@dataclass(frozen=True, slots=True)
class ChangeAreas:
    backend: bool
    frontend: bool
    docs_spec: bool

    @classmethod
    def all(cls) -> ChangeAreas:
        return cls(backend=True, frontend=True, docs_spec=True)


def _normalize(path: str) -> str:
    return PurePosixPath(path.strip().replace("\\", "/")).as_posix()


def classify_paths(paths: list[str]) -> ChangeAreas:
    backend = False
    frontend = False
    docs_spec = False
    saw_path = False
    for raw_path in paths:
        if not raw_path.strip():
            continue
        saw_path = True
        path = _normalize(raw_path)
        if path in GLOBAL_FILES or path.startswith(GLOBAL_PREFIXES):
            return ChangeAreas.all()
        matched = False
        if path.startswith(BACKEND_PREFIXES):
            backend = True
            matched = True
        if path.startswith(FRONTEND_PREFIXES) or path in FRONTEND_FILES:
            frontend = True
            matched = True
        if path.startswith(DOCS_PREFIXES) or path in DOCS_FILES:
            docs_spec = True
            matched = True
        if path in FRONTEND_FILES:
            docs_spec = True
        if not matched:
            return ChangeAreas.all()
    if not saw_path:
        return ChangeAreas.all()
    return ChangeAreas(backend=backend, frontend=frontend, docs_spec=docs_spec)


def _git_changed_paths(event: str, base: str, head: str) -> list[str] | None:
    if not base or not head or base == ZERO_SHA:
        return None
    separator = "..." if event == "pull_request" else ".."
    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{base}{separator}{head}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.splitlines()


def _write_outputs(path: Path, areas: ChangeAreas) -> None:
    path.write_text(
        "\n".join(
            [
                f"backend={str(areas.backend).lower()}",
                f"frontend={str(areas.frontend).lower()}",
                f"docs_spec={str(areas.docs_spec).lower()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--paths-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.event == "workflow_dispatch":
        areas = ChangeAreas.all()
    elif args.paths_file is not None:
        areas = classify_paths(args.paths_file.read_text(encoding="utf-8").splitlines())
    else:
        paths = _git_changed_paths(args.event, args.base, args.head)
        areas = ChangeAreas.all() if paths is None else classify_paths(paths)
    _write_outputs(args.output, areas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 运行测试并确认 GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_ci_tooling.py -q`

Expected: 4 passed。

- [ ] **Step 5: 运行脚本静态检查**

Run:

```powershell
.venv\Scripts\ruff.exe check tests/test_ci_tooling.py ..\..\scripts\ci\detect_changes.py
.venv\Scripts\pyright.exe
```

Expected: Ruff clean，Pyright 0 errors。

---

### Task 2: 安全的 CI 测试配置生成器

**Files:**
- Create: `scripts/ci/prepare_test_config.py`
- Modify: `apps/backend/tests/test_ci_tooling.py`

**Interfaces:**
- Consumes: `--repo-root <path>` 与 `--output-dir <path>`；模板固定来自 `<repo-root>/config`。
- Produces: `<output-dir>/project.json` 与 `<output-dir>/user.project.json`。
- Safety: 任一目标已存在时退出非零；只写固定 `offline-test-key`，不读取环境变量。

- [ ] **Step 1: 写配置生成器的失败测试**

在文件顶部增加 `import json` 和
`PREPARE_CONFIG = REPO_ROOT / "scripts" / "ci" / "prepare_test_config.py"`，然后追加：

```python
def test_ci_config_generation_is_offline_and_refuses_overwrite(tmp_path: Path) -> None:
    output_dir = tmp_path / "config"
    subprocess.run(
        [
            sys.executable,
            str(PREPARE_CONFIG),
            "--repo-root",
            str(REPO_ROOT),
            "--output-dir",
            str(output_dir),
        ],
        check=True,
    )

    project = json.loads((output_dir / "project.json").read_text(encoding="utf-8"))
    user = json.loads((output_dir / "user.project.json").read_text(encoding="utf-8"))
    assert project["backend"]["databaseUrl"].endswith("/agent_py")
    assert project["redis"]["url"].endswith("/0")
    assert user["llm"]["apiKey"] == "offline-test-key"
    assert "sk-" not in json.dumps(user)

    repeated = subprocess.run(
        [
            sys.executable,
            str(PREPARE_CONFIG),
            "--repo-root",
            str(REPO_ROOT),
            "--output-dir",
            str(output_dir),
        ],
        check=False,
    )
    assert repeated.returncode != 0
```

- [ ] **Step 2: 运行并确认 RED**

Expected: FAIL，因为 `prepare_test_config.py` 不存在。

- [ ] **Step 3: 实现生成器**

创建：

```python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast


def _read_object(path: Path) -> dict[str, Any]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Configuration template must be an object: {path}")
    return cast(dict[str, Any], value)


def prepare_test_config(repo_root: Path, output_dir: Path) -> None:
    project_target = output_dir / "project.json"
    user_target = output_dir / "user.project.json"
    if project_target.exists() or user_target.exists():
        raise FileExistsError("CI configuration target already exists.")

    template_dir = repo_root / "config"
    project = _read_object(template_dir / "project.template.json")
    user = _read_object(template_dir / "user.project.template.json")
    llm = user.get("llm")
    if not isinstance(llm, dict):
        raise ValueError("User configuration template is missing llm settings.")
    llm["apiKey"] = "offline-test-key"

    output_dir.mkdir(parents=True, exist_ok=True)
    project_target.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    user_target.write_text(json.dumps(user, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    prepare_test_config(args.repo_root.resolve(), args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 格式化并验证 GREEN**

Run:

```powershell
.venv\Scripts\ruff.exe format ..\..\scripts\ci\prepare_test_config.py
.venv\Scripts\python.exe -m pytest tests/test_ci_tooling.py -q
.venv\Scripts\ruff.exe check tests/test_ci_tooling.py ..\..\scripts\ci
.venv\Scripts\pyright.exe
```

Expected: 5 passed，Ruff clean，Pyright 0 errors。

- [ ] **Step 5: 提交 CI 工具**

```powershell
git add -- scripts/ci/detect_changes.py scripts/ci/prepare_test_config.py apps/backend/tests/test_ci_tooling.py
git commit -m "feat: add deterministic CI tooling"
```

---

### Task 3: GitHub Actions 工作流与契约测试

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `apps/backend/tests/test_ci_tooling.py`

**Interfaces:**
- Consumes: `changes` Job 的 `backend`、`frontend`、`docs_spec` outputs。
- Produces: `backend-quality`、`backend-tests`、`frontend`、`docs-spec` 和显示名固定为 `CI Gate` 的最终状态检查。

- [ ] **Step 1: 写 workflow 契约失败测试**

在文件顶部增加 `import yaml` 和
`CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"`，然后追加：

```python
def test_ci_workflow_has_required_jobs_services_and_safety_guards() -> None:
    workflow_text = CI_WORKFLOW.read_text(encoding="utf-8")
    parsed: object = yaml.safe_load(workflow_text)
    assert isinstance(parsed, dict)
    jobs = parsed.get("jobs")
    assert isinstance(jobs, dict)
    assert set(jobs) == {
        "changes",
        "backend-quality",
        "backend-tests",
        "frontend",
        "docs-spec",
        "ci-gate",
    }

    required_tokens = [
        "pull_request:",
        "workflow_dispatch:",
        "permissions:\n  contents: read",
        "cancel-in-progress: true",
        "postgres:16",
        "redis:7-alpine",
        "uv sync --frozen",
        "uv run ruff check .",
        "uv run pyright",
        "uv run pytest",
        "npm ci",
        "npm run frontend:test",
        "npm run frontend:build",
        "@fission-ai/openspec@1.6.0 validate --all",
        "npm run docs:build",
        "name: CI Gate",
        "if: always()",
    ]
    for token in required_tokens:
        assert token in workflow_text

    assert "pull_request_target" not in workflow_text
    assert "secrets." not in workflow_text
    assert "-m live_llm" not in workflow_text
```

- [ ] **Step 2: 运行并确认 RED**

Expected: FAIL，因为 `.github/workflows/ci.yml` 不存在。

- [ ] **Step 3: 创建 workflow**

实现 `.github/workflows/ci.yml`，必须包含以下结构和命令：

```yaml
name: CI

on:
  pull_request:
    branches: [main]
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

env:
  UV_CACHE_DIR: ${{ github.workspace }}/.cache/uv

jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      backend: ${{ steps.detect.outputs.backend }}
      frontend: ${{ steps.detect.outputs.frontend }}
      docs_spec: ${{ steps.detect.outputs.docs_spec }}
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      - id: detect
        name: Detect affected areas
        env:
          EVENT_NAME: ${{ github.event_name }}
          BASE_SHA: ${{ github.event.pull_request.base.sha || github.event.before }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
        run: >-
          python3 scripts/ci/detect_changes.py
          --event "$EVENT_NAME"
          --base "$BASE_SHA"
          --head "$HEAD_SHA"
          --output "$GITHUB_OUTPUT"

  backend-quality:
    needs: changes
    if: needs.changes.outputs.backend == 'true'
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: apps/backend
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: '3.13'
      - uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b
        with:
          version: '0.11.32'
          enable-cache: true
          cache-dependency-glob: apps/backend/uv.lock
      - run: uv sync --frozen
      - run: uv run ruff check .
      - run: uv run pyright

  backend-tests:
    needs: changes
    if: needs.changes.outputs.backend == 'true'
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: agent_py
          POSTGRES_PASSWORD: agent_py_dev
          POSTGRES_DB: agent_py
        ports: ['5432:5432']
        options: >-
          --health-cmd "pg_isready -U agent_py -d agent_py"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
      redis:
        image: redis:7-alpine
        ports: ['6379:6379']
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        with:
          python-version: '3.13'
      - uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b
        with:
          version: '0.11.32'
          enable-cache: true
          cache-dependency-glob: apps/backend/uv.lock
      - name: Create integration database
        env:
          PGPASSWORD: agent_py_dev
        run: psql -h localhost -U agent_py -d agent_py -v ON_ERROR_STOP=1 -c "CREATE DATABASE agent_py_test"
      - name: Prepare offline configuration
        run: python3 scripts/ci/prepare_test_config.py --repo-root . --output-dir config
      - name: Install backend
        working-directory: apps/backend
        run: uv sync --frozen
      - name: Run offline backend suite
        working-directory: apps/backend
        run: uv run pytest

  frontend:
    needs: changes
    if: needs.changes.outputs.frontend == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v6
        with:
          node-version: '22'
          cache: npm
          cache-dependency-path: package-lock.json
      - run: npm ci
      - run: npm run contracts:typecheck
      - run: npm --workspace packages/api-contracts run test
      - run: npm run frontend:test
      - run: npm run frontend:build

  docs-spec:
    needs: changes
    if: needs.changes.outputs.docs_spec == 'true'
    runs-on: ubuntu-latest
    env:
      OPENSPEC_TELEMETRY: '0'
      DO_NOT_TRACK: '1'
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v6
        with:
          node-version: '22'
          cache: npm
          cache-dependency-path: package-lock.json
      - run: npm ci
      - run: npx --yes @fission-ai/openspec@1.6.0 validate --all
      - run: npm run docs:build

  ci-gate:
    name: CI Gate
    if: always()
    needs: [changes, backend-quality, backend-tests, frontend, docs-spec]
    runs-on: ubuntu-latest
    env:
      RESULTS: >-
        ${{ join(needs.*.result, ',') }}
    steps:
      - name: Require every selected check to pass
        run: |
          echo "Job results: $RESULTS"
          if [[ ",$RESULTS," == *",failure,"* || ",$RESULTS," == *",cancelled,"* ]]; then
            exit 1
          fi
```

- [ ] **Step 4: 运行契约测试并确认 GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_ci_tooling.py -q`

Expected: 6 passed。

- [ ] **Step 5: 验证 YAML 与安全约束**

Run:

```powershell
.venv\Scripts\python.exe -c "from pathlib import Path; import yaml; yaml.safe_load(Path('../../.github/workflows/ci.yml').read_text(encoding='utf-8')); print('workflow-yaml-ok')"
Select-String -Path ..\..\.github\workflows\ci.yml -Pattern 'pull_request_target|secrets\.|-m live_llm'
```

Expected: 输出 `workflow-yaml-ok`；`Select-String` 无匹配。

- [ ] **Step 6: 提交 workflow**

```powershell
git add -- .github/workflows/ci.yml apps/backend/tests/test_ci_tooling.py
git commit -m "ci: add path-aware GitHub Actions checks"
```

---

### Task 4: CI 使用文档

**Files:**
- Modify: `README.md`
- Modify: `apps/backend/README.md`

**Interfaces:**
- Produces: 本地与 GitHub CI 命令、Job 职责、`CI Gate` 分支保护、真实模型隔离说明。

- [ ] **Step 1: 在根 README 增加 CI 章节**

说明：

- PR、`main` push 和手动触发；
- 路径感知的五类检查和固定 `CI Gate`；
- GitHub 仓库 Settings → Branches/Rulesets 中将 `CI Gate` 设置为 required；
- `live_llm` 不属于普通 CI；
- 当前无 CD，部署平台明确后再单独设计。

- [ ] **Step 2: 在后端 README 增加 CI 环境说明**

说明 CI 自动创建 PostgreSQL `agent_py`/`agent_py_test`、Redis `/0`/`/15`、从模板生成
临时离线配置，以及本地等价命令：

```powershell
uv sync --frozen
uv run ruff check .
uv run pyright
uv run pytest
```

- [ ] **Step 3: 验证文档契约**

Run:

```powershell
cd apps/backend
.venv\Scripts\python.exe -m pytest tests/test_ci_tooling.py tests/test_local_development_docs.py -q
cd ..\..
npm run docs:build
```

Expected: pytest 全部通过，VitePress build exit 0。

- [ ] **Step 4: 提交文档**

```powershell
git add -- README.md apps/backend/README.md
git commit -m "docs: explain GitHub Actions CI"
```

---

### Task 5: 完整验证与交付

**Files:**
- Verify only; no planned file changes.

**Interfaces:**
- Produces: 第一版 CI 可合并的本地验证证据；GitHub 执行结果需在仓库推送后获得。

- [ ] **Step 1: 后端静态检查与完整离线测试**

```powershell
cd apps/backend
uv run ruff check .
uv run pyright
uv run pytest
```

Expected: Ruff clean，Pyright 0 errors，完整 pytest 通过且 3 个 `live_llm` 测试 deselected。

- [ ] **Step 2: 前端和共享契约**

```powershell
cd ..\..
npm run contracts:typecheck
npm --workspace packages/api-contracts run test
npm run frontend:test
npm run frontend:build
```

Expected: 全部 exit 0。

- [ ] **Step 3: OpenSpec 与文档**

```powershell
openspec validate --all
npm run docs:build
```

Expected: OpenSpec 0 failed，VitePress build exit 0。

- [ ] **Step 4: 最终仓库检查**

```powershell
git diff --check
git status --short --branch
git log -5 --oneline
```

Expected: 无未提交修改；本地 `config/project.json`、`config/user.project.json` 仍被忽略；
最近提交包含 CI 工具、workflow 和文档。

- [ ] **Step 5: 交付限制说明**

明确说明 workflow 的 YAML、命令和本地契约已验证，但由于当前仓库没有 GitHub remote，
本次无法声称 GitHub-hosted runner 已实际执行。配置远程并推送后，首次 Actions 运行才是
云端验证证据；通过后再将 `CI Gate` 设为 required check。
