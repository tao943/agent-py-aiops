"""Archive-first coordination for durable evaluation run history."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from super_ai.evaluation.history import (
    EvaluationRunEnvelope,
    artifact_checksum,
    running_from_terminal,
)
from super_ai.evaluation.persistence import EvaluationDatabaseUnavailable


class EvaluationArchiveWriter(Protocol):
    def start(self, envelope: EvaluationRunEnvelope) -> Path: ...

    def finalize(self, envelope: EvaluationRunEnvelope) -> Path: ...


class EvaluationDatabaseWriter(Protocol):
    async def start_envelope(self, envelope: EvaluationRunEnvelope) -> object: ...

    async def finalize_envelope(
        self,
        envelope: EvaluationRunEnvelope,
        *,
        artifact_checksum: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class RecordingOutcome:
    """Persistence result without leaking database exception details."""

    database_pending: bool


class EvaluationRunRecorder:
    """Write local artifacts first, then best-effort synchronize PostgreSQL."""

    def __init__(
        self,
        *,
        archive: EvaluationArchiveWriter,
        repository: EvaluationDatabaseWriter,
    ) -> None:
        self._archive = archive
        self._repository = repository

    async def start(self, envelope: EvaluationRunEnvelope) -> RecordingOutcome:
        self._archive.start(envelope)
        try:
            await self._repository.start_envelope(envelope)
        except EvaluationDatabaseUnavailable:
            return RecordingOutcome(database_pending=True)
        return RecordingOutcome(database_pending=False)

    async def finish(self, envelope: EvaluationRunEnvelope) -> RecordingOutcome:
        self._archive.finalize(envelope)
        try:
            await self._repository.start_envelope(running_from_terminal(envelope))
            await self._repository.finalize_envelope(
                envelope,
                artifact_checksum=artifact_checksum(envelope),
            )
        except EvaluationDatabaseUnavailable:
            return RecordingOutcome(database_pending=True)
        return RecordingOutcome(database_pending=False)

    async def fail(self, envelope: EvaluationRunEnvelope) -> RecordingOutcome:
        """Persist a classified terminal failure through the same safe path."""
        return await self.finish(envelope)
