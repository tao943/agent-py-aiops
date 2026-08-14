"""Evaluator-only Snapshot-to-knowledge coverage contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml


@dataclass(frozen=True, slots=True)
class SnapshotKnowledgeCoverage:
    """Generic knowledge documents approved for one Snapshot family."""

    snapshot_id: str
    documents: tuple[str, ...]


def load_snapshot_knowledge_coverage(
    path: Path,
    *,
    scenario_root: Path,
    knowledge_root: Path,
) -> tuple[SnapshotKnowledgeCoverage, ...]:
    """Load the evaluator-only manifest after validating repository references."""
    try:
        payload: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Snapshot knowledge coverage file does not exist: {path}.") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Snapshot knowledge coverage YAML is invalid: {path}.") from exc

    root = _mapping(payload, "Snapshot knowledge coverage")
    raw_coverage = _mapping(root.get("coverage"), "coverage")
    if not raw_coverage:
        raise ValueError("Snapshot knowledge coverage must be a non-empty mapping.")

    scenario_ids = {
        item.name
        for item in scenario_root.iterdir()
        if item.is_dir() and (item / "scenario.yaml").is_file()
    }
    if set(raw_coverage) != scenario_ids:
        raise ValueError("Snapshot knowledge coverage must match repository scenarios exactly.")

    resolved_knowledge_root = knowledge_root.resolve()
    rows: list[SnapshotKnowledgeCoverage] = []
    for snapshot_id, raw_documents in sorted(raw_coverage.items()):
        values = _sequence(raw_documents, f"coverage for {snapshot_id}")
        if not values or not all(isinstance(item, str) and item.strip() for item in values):
            raise ValueError(f"Coverage for {snapshot_id} must contain document names.")
        documents = tuple(cast(str, item).strip() for item in values)
        if len(documents) != len(set(documents)):
            raise ValueError(f"Coverage for {snapshot_id} contains duplicate documents.")
        for document in documents:
            candidate = (resolved_knowledge_root / document).resolve()
            if (
                Path(document).name != document
                or Path(document).suffix.casefold() != ".md"
                or document.casefold().startswith("apy-")
                or not candidate.is_relative_to(resolved_knowledge_root)
            ):
                raise ValueError(f"Coverage document is unsafe: {document}.")
            if not candidate.is_file():
                raise ValueError(f"Coverage document does not exist: {document}.")
        rows.append(SnapshotKnowledgeCoverage(snapshot_id, documents))
    return tuple(rows)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a string-keyed mapping.")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{label} must be a string-keyed mapping.")
    return cast(Mapping[str, object], raw)


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence.")
    return cast(Sequence[object], value)
