"""Atomic worktree-external artifact storage for evaluation runs."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

from super_ai.evaluation.history import EvaluationRunEnvelope, validate_run_id
from super_ai.project_config import ProjectConfigurationError, load_project_config

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]


class EvaluationArchiveError(RuntimeError):
    """The durable local evaluation archive could not be written."""


class EvaluationArchive:
    """Persist versioned run envelopes outside disposable Git worktrees."""

    def __init__(self, root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> None:
        if not root.is_absolute():
            raise ProjectConfigurationError("Evaluation archive path must be absolute.")
        resolved_root = root.resolve(strict=False)
        resolved_repository = repository_root.resolve(strict=False)
        if resolved_root == resolved_repository or resolved_root.is_relative_to(
            resolved_repository
        ):
            raise ProjectConfigurationError(
                "Evaluation archive path must be outside the Git worktree."
            )
        self.root = resolved_root
        self._repository_root = resolved_repository

    @classmethod
    def from_config(
        cls,
        *,
        config_path: Path | str | None = None,
        repository_root: Path = REPOSITORY_ROOT,
    ) -> EvaluationArchive:
        """Load the required archive path exclusively from project JSON configuration."""
        config = load_project_config(config_path)
        section = config.get("evaluation")
        if not isinstance(section, Mapping):
            raise ProjectConfigurationError(
                "Project config section must be an object: evaluation"
            )
        typed_section = cast(Mapping[str, object], section)
        raw_path = typed_section.get("archiveDir")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ProjectConfigurationError(
                "Project config field must be a non-empty string: evaluation.archiveDir"
            )
        return cls(Path(raw_path.strip()), repository_root=repository_root)

    def path_for(self, envelope: EvaluationRunEnvelope) -> Path:
        """Return the canonical path for one run and creation month."""
        validate_run_id(envelope.run_id)
        return (
            self.root
            / envelope.evaluation_kind
            / f"{envelope.created_at.year:04d}"
            / f"{envelope.created_at.month:02d}"
            / f"{envelope.run_id}.json"
        )

    def start(self, envelope: EvaluationRunEnvelope) -> Path:
        """Create or idempotently observe a running run artifact."""
        if envelope.status != "running":
            raise ValueError("Evaluation archive start requires a running envelope.")
        existing = self._find(envelope.run_id)
        if existing is not None:
            stored = self._read(existing)
            if stored == envelope:
                return existing
            if stored.status != "running":
                raise ValueError(
                    f"Evaluation run {envelope.run_id} already has a terminal artifact."
                )
            raise ValueError(f"Evaluation run {envelope.run_id} has a different running identity.")
        path = self.path_for(envelope)
        self._atomic_write(path, envelope)
        return path

    def finalize(self, envelope: EvaluationRunEnvelope) -> Path:
        """Advance one running artifact to exactly one immutable terminal result."""
        if envelope.status == "running":
            raise ValueError("Evaluation archive finalize requires a terminal envelope.")
        existing = self._find(envelope.run_id)
        if existing is None:
            raise ValueError(f"Evaluation run {envelope.run_id} has no running artifact.")
        stored = self._read(existing)
        if stored.status != "running":
            if stored == envelope:
                return existing
            raise ValueError(f"Evaluation run {envelope.run_id} already has a terminal artifact.")
        if not _same_identity(stored, envelope):
            raise ValueError(f"Evaluation run {envelope.run_id} has a different running identity.")
        expected_path = self.path_for(envelope)
        if expected_path != existing:
            raise ValueError("Evaluation artifact path does not match its stable identity.")
        self._atomic_write(existing, envelope)
        return existing

    def load(self, run_id: str) -> EvaluationRunEnvelope:
        """Load exactly one canonical run by its safe ID."""
        validate_run_id(run_id)
        path = self._find(run_id)
        if path is None:
            raise FileNotFoundError(f"Evaluation artifact does not exist: {run_id}")
        return self._read(path)

    def iter_envelopes(self) -> Iterator[EvaluationRunEnvelope]:
        """Iterate canonical artifacts in deterministic path order."""
        if not self.root.exists():
            return
        for path in sorted(self.root.glob("*/*/*/*.json")):
            envelope = self._read(path)
            if self.path_for(envelope) != path.resolve(strict=False):
                raise ValueError(f"Evaluation artifact escaped canonical layout: {path}")
            yield envelope

    def _find(self, run_id: str) -> Path | None:
        validate_run_id(run_id)
        if not self.root.exists():
            return None
        matches = sorted(self.root.glob(f"*/*/*/{run_id}.json"))
        if len(matches) > 1:
            raise ValueError(f"Evaluation run {run_id} has multiple artifact paths.")
        return matches[0].resolve(strict=False) if matches else None

    def _read(self, path: Path) -> EvaluationRunEnvelope:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Evaluation artifact must contain a JSON object.")
        return EvaluationRunEnvelope.from_json(cast(Mapping[str, object], raw))

    def _atomic_write(self, path: Path, envelope: EvaluationRunEnvelope) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    envelope.to_json(),
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise EvaluationArchiveError("Evaluation archive write failed.") from exc
        finally:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass


def _same_identity(running: EvaluationRunEnvelope, terminal: EvaluationRunEnvelope) -> bool:
    return (
        running.run_id == terminal.run_id
        and running.evaluation_kind == terminal.evaluation_kind
        and running.scenario_id == terminal.scenario_id
        and running.suite_version == terminal.suite_version
        and running.metadata == terminal.metadata
        and running.provenance == terminal.provenance
        and running.created_at == terminal.created_at
        and running.started_at == terminal.started_at
    )
