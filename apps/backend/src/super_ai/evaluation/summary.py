"""Deterministic, answer-isolated summaries of canonical evaluation history."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from super_ai.evaluation.artifacts import InvestigationBenchmarkMetrics
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


@dataclass(frozen=True, slots=True)
class InvestigationStrategyComparison:
    pair_count: int
    single_average_score: int
    multi_average_score: int
    single_p95_duration_ms: int
    multi_p95_duration_ms: int
    evidence_recall_gain_basis_points: int
    root_cause_top1_gain_basis_points: int
    maximum_extra_model_calls: int
    maximum_duplicate_evidence_basis_points: int
    performance_gate_passed: bool
    capability_gate_passed: bool
    security_gate_passed: bool
    eligibility: Literal["benchmark_only", "eligible_for_default_review"]
    reason_codes: tuple[str, ...]


def compare_investigation_strategies(
    single_runs: Sequence[InvestigationBenchmarkMetrics],
    multi_runs: Sequence[InvestigationBenchmarkMetrics],
) -> InvestigationStrategyComparison:
    """Compare persisted safe A/B metrics without evaluator-private inputs."""
    pairs = _pair_investigation_runs(single_runs, multi_runs)
    if not pairs:
        raise ValueError("Investigation comparison requires paired runs.")
    singles = [single for single, _ in pairs]
    multis = [multi for _, multi in pairs]
    single_root_rate = _boolean_rate_basis_points(
        [item.root_cause_top1_correct for item in singles]
    )
    multi_root_rate = _boolean_rate_basis_points(
        [item.root_cause_top1_correct for item in multis]
    )
    single_recall = _integer_average(
        [item.evidence_recall_basis_points for item in singles]
    )
    multi_recall = _integer_average(
        [item.evidence_recall_basis_points for item in multis]
    )
    single_p95 = _nearest_rank_p95([item.duration_ms for item in singles])
    multi_p95 = _nearest_rank_p95([item.duration_ms for item in multis])
    maximum_extra_calls = max(
        multi.model_call_count - single.model_call_count
        for single, multi in pairs
    )
    maximum_duplicates = max(
        item.duplicate_evidence_basis_points for item in multis
    )
    security_passed = all(
        item.security_hard_gate_passed for item in (*singles, *multis)
    )
    performance_passed = (
        multi_p95 * 2 <= single_p95 * 3
        and maximum_extra_calls <= 2
        and maximum_duplicates <= 1_000
    )
    recall_gain = multi_recall - single_recall
    root_gain = multi_root_rate - single_root_rate
    capability_passed = recall_gain >= 1_000 or root_gain >= 500
    reasons: list[str] = []
    if not security_passed:
        reasons.append("security_hard_gate_failed")
    if multi_p95 * 2 > single_p95 * 3:
        reasons.append("multi_p95_latency_exceeded")
    if maximum_extra_calls > 2:
        reasons.append("extra_model_call_budget_exceeded")
    if maximum_duplicates > 1_000:
        reasons.append("duplicate_evidence_rate_exceeded")
    if not capability_passed:
        reasons.append("capability_gain_missing")
    eligible = security_passed and performance_passed and capability_passed
    return InvestigationStrategyComparison(
        pair_count=len(pairs),
        single_average_score=_integer_average(
            [item.total_score for item in singles]
        ),
        multi_average_score=_integer_average([item.total_score for item in multis]),
        single_p95_duration_ms=single_p95,
        multi_p95_duration_ms=multi_p95,
        evidence_recall_gain_basis_points=recall_gain,
        root_cause_top1_gain_basis_points=root_gain,
        maximum_extra_model_calls=maximum_extra_calls,
        maximum_duplicate_evidence_basis_points=maximum_duplicates,
        performance_gate_passed=performance_passed,
        capability_gate_passed=capability_passed,
        security_gate_passed=security_passed,
        eligibility=(
            "eligible_for_default_review" if eligible else "benchmark_only"
        ),
        reason_codes=tuple(reasons),
    )


def _pair_investigation_runs(
    single_runs: Sequence[InvestigationBenchmarkMetrics],
    multi_runs: Sequence[InvestigationBenchmarkMetrics],
) -> list[tuple[InvestigationBenchmarkMetrics, InvestigationBenchmarkMetrics]]:
    grouped_single: dict[
        tuple[str, str | None], list[InvestigationBenchmarkMetrics]
    ] = defaultdict(list)
    grouped_multi: dict[
        tuple[str, str | None], list[InvestigationBenchmarkMetrics]
    ] = defaultdict(list)
    for item in single_runs:
        _validate_comparison_run(item, expected_strategy="single")
        grouped_single[(item.scenario_id, item.campaign_id)].append(item)
    for item in multi_runs:
        _validate_comparison_run(item, expected_strategy="multi")
        grouped_multi[(item.scenario_id, item.campaign_id)].append(item)
    if set(grouped_single) != set(grouped_multi):
        raise ValueError("Investigation comparison scenario sets do not match.")
    pairs: list[
        tuple[InvestigationBenchmarkMetrics, InvestigationBenchmarkMetrics]
    ] = []
    for key in sorted(grouped_single, key=lambda item: (item[0], item[1] or "")):
        singles = sorted(grouped_single[key], key=lambda item: item.run_id)
        multis = sorted(grouped_multi[key], key=lambda item: item.run_id)
        if len(singles) != len(multis):
            raise ValueError("Investigation comparison run counts do not match.")
        pairs.extend(zip(singles, multis, strict=True))
    return pairs


def _validate_comparison_run(
    item: InvestigationBenchmarkMetrics, *, expected_strategy: Literal["single", "multi"]
) -> None:
    if item.strategy != expected_strategy:
        raise ValueError("Investigation comparison strategy is invalid.")
    if (
        expected_strategy == "multi"
        and item.effective_strategy != "multi_agent"
    ) or (
        expected_strategy == "single"
        and item.effective_strategy == "multi_agent"
    ):
        raise ValueError("Investigation comparison effective strategy is invalid.")
    if not item.run_id or not item.scenario_id:
        raise ValueError("Investigation comparison identity is incomplete.")


def _integer_average(values: Sequence[int]) -> int:
    return round(sum(values) / len(values))


def _boolean_rate_basis_points(values: Sequence[bool]) -> int:
    return round(sum(values) * 10_000 / len(values))


def _nearest_rank_p95(values: Sequence[int]) -> int:
    ordered = sorted(values)
    rank = max(1, (95 * len(ordered) + 99) // 100)
    return ordered[rank - 1]


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
