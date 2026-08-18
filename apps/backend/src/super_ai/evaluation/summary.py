"""Deterministic, answer-isolated summaries of canonical evaluation history."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from super_ai.evaluation.history import EvaluationRunEnvelope, artifact_checksum


@dataclass(frozen=True, slots=True)
class HistoryCounts:
    total: int
    reconstructed: int
    database_pending: int
    by_kind: Mapping[str, int]
    by_status: Mapping[str, int]
    by_provenance: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ReconciliationCounts:
    archive_only: int
    database_only: int
    synchronized: int
    null_checksum: int
    conflicts: int


@dataclass(frozen=True, slots=True)
class HistorySummary:
    counts: HistoryCounts
    reconciliation: ReconciliationCounts
    markdown: str
    index_rows: tuple[dict[str, object], ...]


def build_history_summary(
    envelopes: Sequence[EvaluationRunEnvelope],
    *,
    database_checksums: Mapping[str, str | None],
) -> HistorySummary:
    ordered = sorted(envelopes, key=lambda item: (item.created_at, item.run_id))
    archive_ids = {item.run_id for item in ordered}
    archive_only = synchronized = null_checksum = conflicts = 0
    pending_ids: set[str] = set()
    rows: list[dict[str, object]] = []
    for envelope in ordered:
        checksum = artifact_checksum(envelope)
        database_checksum = database_checksums.get(envelope.run_id, _MISSING)
        if database_checksum is _MISSING:
            archive_only += 1
            pending_ids.add(envelope.run_id)
            state = "archive_only"
        elif database_checksum is None:
            null_checksum += 1
            pending_ids.add(envelope.run_id)
            state = "null_checksum"
        elif database_checksum == checksum:
            synchronized += 1
            state = "synchronized"
        else:
            conflicts += 1
            state = "conflict"
        rows.append(
            {
                "runId": envelope.run_id,
                "evaluationKind": envelope.evaluation_kind,
                "scenarioId": envelope.scenario_id,
                "status": envelope.status,
                "validity": envelope.validity,
                "passed": envelope.passed,
                "provenance": envelope.provenance,
                "createdAt": envelope.created_at.isoformat().replace("+00:00", "Z"),
                "artifactChecksum": checksum,
                "reconciliation": state,
                "metrics": dict(envelope.metrics),
            }
        )
    database_only = len(set(database_checksums).difference(archive_ids))
    kind_counts = Counter(item.evaluation_kind for item in ordered)
    status_counts = Counter(item.status for item in ordered)
    provenance_counts = Counter(item.provenance for item in ordered)
    counts = HistoryCounts(
        total=len(ordered),
        reconstructed=provenance_counts.get("reconstructed", 0),
        database_pending=len(pending_ids),
        by_kind=dict(sorted(kind_counts.items())),
        by_status=dict(sorted(status_counts.items())),
        by_provenance=dict(sorted(provenance_counts.items())),
    )
    reconciliation = ReconciliationCounts(
        archive_only=archive_only,
        database_only=database_only,
        synchronized=synchronized,
        null_checksum=null_checksum,
        conflicts=conflicts,
    )
    markdown = _markdown(counts, reconciliation, ordered)
    return HistorySummary(counts, reconciliation, markdown, tuple(rows))


def write_history_summary(root: Path, summary: HistorySummary) -> None:
    """Atomically replace the rebuildable index and human-readable summary."""
    index = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in summary.index_rows
    )
    _atomic_text(root / "index.jsonl", index)
    _atomic_text(root / "summary.md", summary.markdown)


def _markdown(
    counts: HistoryCounts,
    reconciliation: ReconciliationCounts,
    envelopes: Sequence[EvaluationRunEnvelope],
) -> str:
    recall_values = [
        float(value)
        for item in envelopes
        for value in (item.metrics.get("recallAt1"),)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    lines = [
        "# Evaluation History",
        "",
        f"- 总运行数：{counts.total}",
        f"- 重建记录：{counts.reconstructed}",
        f"- 数据库待同步：{counts.database_pending}",
        f"- Archive-only：{reconciliation.archive_only}",
        f"- Database-only：{reconciliation.database_only}",
        f"- Checksum 冲突：{reconciliation.conflicts}",
    ]
    if recall_values:
        lines.append(f"- Recall@1 平均值：{sum(recall_values) / len(recall_values):.4f}")
    lines.extend(
        [
            "",
            "## 不可恢复边界",
            "",
            "缺失的原始评分 Artifact、私有答案和原始模型响应不会被推断或补造。",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


_MISSING = object()
