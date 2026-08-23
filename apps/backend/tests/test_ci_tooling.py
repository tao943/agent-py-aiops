from __future__ import annotations

import json
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DETECT_CHANGES = REPO_ROOT / "scripts" / "ci" / "detect_changes.py"
PREPARE_CONFIG = REPO_ROOT / "scripts" / "ci" / "prepare_test_config.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
WIKI_SYNC = REPO_ROOT / ".codex" / "skills" / "wiki-sync" / "scripts" / "sync_wiki.py"


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
        "gateway": "false",
    }
    assert _run_change_detection(tmp_path, ["apps/frontend/src/App.vue"]) == {
        "backend": "false",
        "frontend": "true",
        "docs_spec": "false",
        "gateway": "false",
    }
    assert _run_change_detection(tmp_path, ["docs/index.md"]) == {
        "backend": "false",
        "frontend": "false",
        "docs_spec": "true",
        "gateway": "false",
    }
    assert _run_change_detection(tmp_path, ["infra/nginx/default.conf"]) == {
        "backend": "true",
        "frontend": "false",
        "docs_spec": "false",
        "gateway": "true",
    }


def test_ci_change_detection_expands_shared_and_unknown_changes(tmp_path: Path) -> None:
    all_areas = {
        "backend": "true",
        "frontend": "true",
        "docs_spec": "true",
        "gateway": "true",
    }
    assert _run_change_detection(tmp_path, [".github/workflows/ci.yml"]) == all_areas
    assert _run_change_detection(tmp_path, ["unclassified-root-file.txt"]) == all_areas


def test_ci_change_detection_combines_multiple_areas(tmp_path: Path) -> None:
    assert _run_change_detection(
        tmp_path,
        ["apps/backend/pyproject.toml", "packages/api-contracts/src/index.ts"],
    ) == {
        "backend": "true",
        "frontend": "true",
        "docs_spec": "false",
        "gateway": "false",
    }


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
        "gateway=true",
    ]


def test_wiki_sync_accepts_resolved_directory_link_not_placeholder(tmp_path: Path) -> None:
    target = tmp_path / "openspec"
    target.mkdir()
    placeholder = tmp_path / "materialized-link"
    placeholder.write_text("../openspec\n", encoding="utf-8")
    namespace = runpy.run_path(str(WIKI_SYNC))
    openspec_link_is_valid = cast(
        Callable[[Path, Path], bool], namespace["openspec_link_is_valid"]
    )

    assert openspec_link_is_valid(target, target) is True
    assert openspec_link_is_valid(placeholder, target) is False


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


def test_ci_workflow_has_required_jobs_services_and_safety_guards() -> None:
    workflow_text = CI_WORKFLOW.read_text(encoding="utf-8")
    parsed: object = yaml.safe_load(workflow_text)
    assert isinstance(parsed, dict)
    workflow = cast(dict[str, object], parsed)
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    job_map = cast(dict[str, object], jobs)
    assert set(job_map) == {
        "changes",
        "backend-quality",
        "backend-tests",
        "auto-closure-contracts",
        "frontend",
        "docs-spec",
        "gateway",
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
        "Run focused automatic-closure contracts",
        "npm ci",
        "npm run frontend:test",
        "npm run frontend:build",
        "@fission-ai/openspec@1.6.0 validate --all",
        "npm run docs:build",
        "docker compose -f infra/compose.yaml config",
        "docker compose -f infra/compose.yaml run --rm --no-deps nginx nginx -t",
        "name: CI Gate",
        "if: always()",
    ]
    for token in required_tokens:
        assert token in workflow_text

    assert "pull_request_target" not in workflow_text
    assert "secrets." not in workflow_text
    assert "-m live_llm" not in workflow_text


def test_ci_jobs_bootstrap_ignored_runtime_inputs() -> None:
    parsed: object = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    workflow = cast(dict[str, object], parsed)
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    job_map = cast(dict[str, object], jobs)

    def job_runs(job_name: str) -> list[str]:
        job = job_map.get(job_name)
        assert isinstance(job, dict)
        steps = cast(dict[str, object], job).get("steps")
        assert isinstance(steps, list)
        runs: list[str] = []
        for raw_step in cast(list[object], steps):
            assert isinstance(raw_step, dict)
            run = cast(dict[str, object], raw_step).get("run")
            if isinstance(run, str):
                runs.append(run)
        return runs

    backend_runs = job_runs("backend-tests")
    assert "mkdir -p apps/backend/var" in backend_runs
    assert backend_runs.index("mkdir -p apps/backend/var") < backend_runs.index(
        "uv run pytest"
    )

    frontend_runs = job_runs("frontend")
    prepare_runs = [run for run in frontend_runs if "prepare_test_config.py" in run]
    assert prepare_runs == [
        "python3 scripts/ci/prepare_test_config.py --repo-root . --output-dir config"
    ]
    assert frontend_runs.index(prepare_runs[0]) < frontend_runs.index(
        "npm run frontend:test"
    )
