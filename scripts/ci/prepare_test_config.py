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
    project_target.write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    user_target.write_text(
        json.dumps(user, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    prepare_test_config(args.repo_root.resolve(), args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
