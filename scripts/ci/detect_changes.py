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
