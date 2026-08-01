from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DETECT_CHANGES = REPO_ROOT / "scripts" / "ci" / "detect_changes.py"
PREPARE_CONFIG = REPO_ROOT / "scripts" / "ci" / "prepare_test_config.py"


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
