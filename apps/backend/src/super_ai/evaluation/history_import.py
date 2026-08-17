"""Explicit-source import and reconciliation for evaluation history."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, cast

from super_ai.evaluation.archive import EvaluationArchive
from super_ai.evaluation.history import (
    EvaluationKind,
    EvaluationProvenance,
    EvaluationRunEnvelope,
    EvaluationStatus,
    artifact_checksum,
    interrupted_envelope,
    running_envelope,
    running_from_terminal,
    terminal_envelope,
)
from super_ai.evaluation.persistence import EvaluationDatabaseUnavailable
from super_ai.memory.repositories import EvaluationResultRecord, EvaluationRunRecord

_FORBIDDEN = frozenset(
    {
        "apikey", "accesskey", "secret", "secretkey", "password", "token",
        "oracle", "groundtruth", "primarycause", "answerkey", "prompt", "chainofthought",
    }
)


class HistoryRepository(Protocol):
    async def start_envelope(self, envelope: EvaluationRunEnvelope) -> object: ...

    async def finalize_envelope(
        self, envelope: EvaluationRunEnvelope, *, artifact_checksum: str
    ) -> object: ...

    async def attach_artifact_checksum(
        self, *, run_id: str, artifact_checksum: str
    ) -> object: ...

    async def list_runs_with_results(
        self,
    ) -> list[tuple[EvaluationRunRecord, EvaluationResultRecord | None]]: ...


@dataclass(frozen=True, slots=True)
class HistoryImportEntry:
    source: str
    run_id: str | None
    status: str


@dataclass(frozen=True, slots=True)
class HistoryImportReport:
    imported: int = 0
    duplicates: int = 0
    reconstructed: int = 0
    rejected: int = 0
    conflicts: int = 0
    database_pending: int = 0
    entries: tuple[HistoryImportEntry, ...] = ()


async def import_history(
    *,
    sources: Sequence[Path],
    archive: EvaluationArchive,
    repository: HistoryRepository,
) -> HistoryImportReport:
    count_names = (
        "imported", "duplicates", "reconstructed", "rejected", "conflicts",
        "database_pending",
    )
    counts = {key: 0 for key in count_names}
    entries: list[HistoryImportEntry] = []
    for source, path in _source_files(sources):
        source_root = source.resolve(strict=True)
        if source_root.is_dir() and not path.resolve(strict=True).is_relative_to(source_root):
            counts["rejected"] += 1
            entries.append(HistoryImportEntry(str(path), None, "rejected"))
            continue
        try:
            raw_bytes = path.read_bytes()
            raw: object = json.loads(raw_bytes)
            _reject_forbidden(raw)
            envelopes = _decode_history(
                raw,
                source_path=path,
                source_checksum=hashlib.sha256(raw_bytes).hexdigest(),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            counts["rejected"] += 1
            entries.append(HistoryImportEntry(str(path), None, "rejected"))
            continue
        for envelope in envelopes:
            try:
                existing = archive.load(envelope.run_id)
            except FileNotFoundError:
                if envelope.status == "running":
                    archive.start(envelope)
                else:
                    archive.start(running_from_terminal(envelope))
                    archive.finalize(envelope)
                counts["imported"] += 1
                state = "imported"
            else:
                if artifact_checksum(existing) != artifact_checksum(envelope):
                    counts["conflicts"] += 1
                    entries.append(HistoryImportEntry(str(path), envelope.run_id, "conflict"))
                    continue
                counts["duplicates"] += 1
                state = "duplicate"
            try:
                await repository.start_envelope(running_from_terminal(envelope))
                if envelope.status != "running":
                    await repository.finalize_envelope(
                        envelope, artifact_checksum=artifact_checksum(envelope)
                    )
            except EvaluationDatabaseUnavailable:
                counts["database_pending"] += 1
                state = "database_pending"
            entries.append(HistoryImportEntry(str(path), envelope.run_id, state))
        del source
    return HistoryImportReport(entries=tuple(entries), **counts)


async def reconcile_history(
    *,
    archive: EvaluationArchive,
    repository: HistoryRepository,
    stale_after: timedelta = timedelta(hours=6),
) -> HistoryImportReport:
    now = datetime.now(timezone.utc)
    entries: list[HistoryImportEntry] = []
    pending = conflicts = 0
    for stored in list(archive.iter_envelopes()):
        envelope = stored
        if stored.status == "running" and now - stored.started_at > stale_after:
            envelope = interrupted_envelope(
                stored,
                completed_at=now,
                failure_category="stale_running_record",
            )
            archive.finalize(envelope)
        elif stored.status == "running":
            continue
        try:
            await repository.start_envelope(running_from_terminal(envelope))
            await repository.finalize_envelope(
                envelope, artifact_checksum=artifact_checksum(envelope)
            )
        except EvaluationDatabaseUnavailable:
            pending += 1
            state = "database_pending"
        except ValueError:
            conflicts += 1
            state = "conflict"
        else:
            state = envelope.status
        entries.append(HistoryImportEntry("archive", envelope.run_id, state))
    list_runs = getattr(repository, "list_runs_with_results", None)
    if list_runs is not None:
        database_rows = await list_runs()
        for run, result in database_rows:
            try:
                archive_envelope = archive.load(run.run_id)
            except FileNotFoundError:
                try:
                    archive_envelope = _database_envelope(run, result)
                    if archive_envelope.status == "running":
                        archive.start(archive_envelope)
                    else:
                        archive.start(running_from_terminal(archive_envelope))
                        archive.finalize(archive_envelope)
                except ValueError:
                    conflicts += 1
                    entries.append(HistoryImportEntry("database", run.run_id, "conflict"))
                    continue
            checksum = artifact_checksum(archive_envelope)
            if run.artifact_checksum not in {None, checksum}:
                conflicts += 1
                entries.append(HistoryImportEntry("database", run.run_id, "conflict"))
                continue
            if run.artifact_checksum is None:
                try:
                    await repository.attach_artifact_checksum(
                        run_id=run.run_id, artifact_checksum=checksum
                    )
                except EvaluationDatabaseUnavailable:
                    pending += 1
                    entries.append(
                        HistoryImportEntry("database", run.run_id, "database_pending")
                    )
                    continue
            entries.append(HistoryImportEntry("database", run.run_id, "synchronized"))
    return HistoryImportReport(
        conflicts=conflicts,
        database_pending=pending,
        entries=tuple(entries),
    )


def _source_files(sources: Sequence[Path]):
    for source in sources:
        root = source.resolve(strict=True)
        paths = [root] if root.is_file() else sorted(root.rglob("*.json"))
        for path in paths:
            resolved = path.resolve(strict=True)
            if root.is_dir() and not resolved.is_relative_to(root):
                yield source, path
                continue
            yield source, resolved


def _decode_history(
    raw: object, *, source_path: Path, source_checksum: str
) -> list[EvaluationRunEnvelope]:
    if not isinstance(raw, Mapping):
        raise ValueError("History source must be a JSON object.")
    payload = cast(Mapping[str, object], raw)
    if "artifactSchemaVersion" in payload:
        envelope = EvaluationRunEnvelope.from_json(payload)
        metadata = dict(envelope.metadata)
        metadata["importSource"] = {"path": str(source_path), "checksum": source_checksum}
        imported = replace(envelope, metadata=metadata, provenance="imported")
        return [EvaluationRunEnvelope.from_json(imported.to_json())]
    if isinstance(payload.get("runs"), list) and isinstance(payload.get("scenario"), str):
        return _snapshot_envelopes(payload, source_path, source_checksum)
    if isinstance(payload.get("metrics"), Mapping):
        return [_retrieval_envelope(payload, source_path, source_checksum)]
    if isinstance(payload.get("scenarioId"), str) and isinstance(payload.get("result"), Mapping):
        return [_live_envelope(payload, source_path, source_checksum)]
    raise ValueError("Unrecognized evaluation history format.")


def _snapshot_envelopes(
    payload: Mapping[str, object], source_path: Path, checksum: str
) -> list[EvaluationRunEnvelope]:
    now = _file_time(source_path)
    scenario = cast(str, payload["scenario"])
    runs = cast(list[object], payload["runs"])
    envelopes: list[EvaluationRunEnvelope] = []
    for index, item in enumerate(runs):
        if not isinstance(item, Mapping):
            raise ValueError("Snapshot run is invalid.")
        run = cast(Mapping[str, object], item)
        run_id = str(run.get("runId") or f"import-{checksum[:24]}-{index}")
        running = running_envelope(
            run_id=run_id,
            evaluation_kind="snapshot",
            scenario_id=scenario,
            suite_version=str(payload.get("suiteVersion") or "v1"),
            metadata={
                "ragMode": str(payload.get("ragMode") or "unknown"),
                "importSource": {"path": str(source_path), "checksum": checksum},
            },
            provenance="imported",
            created_at=now,
            started_at=now,
        )
        raw_dimensions = run.get("dimensions")
        dimensions = (
            cast(Mapping[str, object], raw_dimensions)
            if isinstance(raw_dimensions, Mapping)
            else cast(Mapping[str, object], {})
        )
        metrics = dict(dimensions)
        metrics.update({"total": run.get("total"), "rawTotal": run.get("rawTotal")})
        passed = run.get("passed") is True
        raw_failures = run.get("failures")
        failures = (
            [str(item) for item in cast(Sequence[object], raw_failures)]
            if isinstance(raw_failures, Sequence) and not isinstance(raw_failures, str)
            else []
        )
        envelopes.append(terminal_envelope(
            running=running, status="passed" if passed else "failed",
            validity=str(run.get("validity") or ("VALID_PASS" if passed else "VALID_FAIL")),
            passed=passed, metrics=metrics,
            result_payload={"failures": failures},
            diagnostic_task_id=None, failure_category=None, completed_at=now,
        ))
    return envelopes


def _retrieval_envelope(
    payload: Mapping[str, object], path: Path, checksum: str
) -> EvaluationRunEnvelope:
    now = _file_time(path)
    run_id = str(payload.get("runId") or f"import-{checksum[:32]}")
    metrics = dict(cast(Mapping[str, object], payload["metrics"]))
    passed = _retrieval_passes(metrics)
    running = running_envelope(
        run_id=run_id, evaluation_kind="retrieval", scenario_id=path.stem,
        suite_version="v1", provenance="imported", created_at=now, started_at=now,
        metadata={
            "modelConfiguration": dict(cast(Mapping[str, object], payload.get("models") or {})),
            "datasetChecksum": checksum,
            "ownerUserId": str(payload.get("ownerUserId") or "unknown"),
            "knowledgeBaseId": str(payload.get("knowledgeBaseId") or "unknown"),
            "importSource": {"path": str(path), "checksum": checksum},
        },
    )
    return terminal_envelope(
        running=running, status="passed" if passed else "failed",
        validity="VALID_PASS" if passed else "VALID_FAIL", passed=passed,
        metrics=metrics, result_payload={"failures": []}, diagnostic_task_id=None,
        failure_category=None, completed_at=now,
    )


def _live_envelope(
    payload: Mapping[str, object], path: Path, checksum: str
) -> EvaluationRunEnvelope:
    now = _file_time(path)
    result = cast(Mapping[str, object], payload["result"])
    status = str(payload.get("status") or "infra_invalid")
    if status not in {"passed", "failed", "infra_invalid"}:
        status = "infra_invalid"
    running = running_envelope(
        run_id=str(payload.get("runId") or f"import-{checksum[:32]}"),
        evaluation_kind="live", scenario_id=str(payload["scenarioId"]), suite_version="v1",
        provenance="imported", created_at=now, started_at=now,
        metadata={
            "evidenceSource": str(result.get("evidenceSource") or "local"),
            "importSource": {"path": str(path), "checksum": checksum},
        },
    )
    metrics = {
        key: result[key]
        for key in ("total", "rawTotal", "verificationPassed", "cleanupSucceeded")
        if key in result
    }
    result_payload = {
        key: result[key]
        for key in ("failures", "hardGate", "failureStage", "authorizationCode")
        if key in result
    }
    return terminal_envelope(
        running=running, status=cast(EvaluationStatus, status),
        validity=str(result.get("validity") or "INFRA_INVALID"),
        passed=True if status == "passed" else (False if status == "failed" else None),
        metrics=metrics, result_payload=result_payload, diagnostic_task_id=None,
        failure_category=cast(str | None, result.get("failureCategory")), completed_at=now,
    )


def _reject_forbidden(value: object) -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, item in mapping.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in _FORBIDDEN:
                raise ValueError("History source contains a forbidden field.")
            _reject_forbidden(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        for item in sequence:
            _reject_forbidden(item)


def _file_time(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _retrieval_passes(metrics: Mapping[str, object]) -> bool:
    def number(key: str) -> float:
        value = metrics.get(key)
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else -1.0
        )
    return (
        number("recallAt1") >= 0.80 and number("recallAt3") >= 0.95
        and number("mrr") >= 0.85 and 0 <= number("forbiddenTopOneRate") <= 0.05
        and number("citationCompletenessRate") == 1.0
    )


def _database_envelope(
    run: EvaluationRunRecord,
    result: EvaluationResultRecord | None,
) -> EvaluationRunEnvelope:
    metadata = dict(run.run_metadata)
    if run.evaluation_kind == "snapshot" and not metadata:
        git_sha = run.agent_version.get("git_sha")
        workflow = run.agent_version.get("workflow_version")
        if isinstance(git_sha, str):
            metadata["gitSha"] = git_sha
        if isinstance(workflow, str):
            metadata["workflowVersion"] = workflow
        metadata["modelConfiguration"] = dict(run.model_configuration)
        rag_mode = run.model_configuration.get("rag_mode")
        if isinstance(rag_mode, str):
            metadata["ragMode"] = rag_mode
    started_at = run.started_at or run.created_at
    running = running_envelope(
        run_id=run.run_id,
        evaluation_kind=cast(EvaluationKind, run.evaluation_kind),
        scenario_id=run.scenario_id,
        suite_version=run.suite_version,
        metadata=metadata,
        provenance=cast(EvaluationProvenance, run.provenance),
        created_at=run.created_at,
        started_at=started_at,
    )
    if run.status in {"running", "pending"} and result is None:
        return running
    status = run.status
    if status == "completed":
        status = "passed" if result is not None and result.passed else "failed"
    elif status == "infra_failed":
        status = "infra_invalid"
    if status not in {"passed", "failed", "agent_failed", "infra_invalid", "interrupted"}:
        status = "infra_invalid"
    metrics = dict(result.metrics) if result is not None else {}
    result_payload = dict(result.result_payload) if result is not None else {}
    if result is not None and run.evaluation_kind == "snapshot" and not metrics:
        metrics = dict(result.dimension_scores)
        metrics["total"] = result.total
        metrics["rawTotal"] = result.raw_total
    if result is not None and run.evaluation_kind == "snapshot" and not result_payload:
        result_payload = {
            "failures": list(result.failures),
            "scoreReasons": list(result.score_reasons),
            "hardGate": result.hard_gate,
        }
    return terminal_envelope(
        running=running,
        status=cast(EvaluationStatus, status),
        validity=result.validity if result is not None else "INFRA_INVALID",
        passed=result.passed if result is not None else None,
        metrics=metrics,
        result_payload=result_payload,
        diagnostic_task_id=run.diagnostic_task_id,
        failure_category=run.failure_category,
        completed_at=run.completed_at or started_at,
    )
