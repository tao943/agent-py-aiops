from dataclasses import replace
from datetime import datetime, timezone

import pytest

from super_ai.evaluation.artifacts import InvestigationBenchmarkMetrics
from super_ai.evaluation.history import (
    EvaluationKind,
    EvaluationProvenance,
    EvaluationRunEnvelope,
    artifact_checksum,
    running_envelope,
    terminal_envelope,
)
from super_ai.evaluation.summary import (
    build_history_summary,
    compare_investigation_strategies,
)


def envelope(
    run_id: str,
    *,
    kind: EvaluationKind = "retrieval",
    provenance: EvaluationProvenance = "native",
) -> EvaluationRunEnvelope:
    now = datetime(2026, 8, 17, tzinfo=timezone.utc)
    metadata = (
        {"workflowVersion": "retrieval-v1", "datasetChecksum": "a" * 64}
        if kind == "retrieval"
        else {"workflowVersion": "live-v1", "evidenceSource": "local"}
    )
    running = running_envelope(
        run_id=run_id,
        evaluation_kind=kind,
        scenario_id="suite",
        suite_version="v1",
        metadata=metadata,
        provenance=provenance,
        created_at=now,
        started_at=now,
    )
    metrics = {"recallAt1": 0.8} if kind == "retrieval" else {}
    return terminal_envelope(
        running=running,
        status="passed",
        validity="VALID_PASS",
        passed=True,
        metrics=metrics,
        result_payload={"failures": []},
        diagnostic_task_id=None,
        failure_category=None,
        completed_at=now,
    )


def test_summary_distinguishes_native_reconstructed_and_pending() -> None:
    first = envelope("eval-1")
    second = envelope("eval-2", kind="live", provenance="reconstructed")
    summary = build_history_summary(
        [first, second],
        database_checksums={"eval-1": artifact_checksum(first)},
    )
    assert summary.counts.total == 2
    assert summary.counts.reconstructed == 1
    assert summary.counts.database_pending == 1
    assert "Recall@1" in summary.markdown
    assert "不可恢复边界" in summary.markdown


def test_summary_detects_all_checksum_reconciliation_states() -> None:
    archive_only = envelope("archive-only")
    same = envelope("same")
    different = envelope("different")
    summary = build_history_summary(
        [archive_only, same, different],
        database_checksums={
            "db-only": "d" * 64,
            "same": artifact_checksum(same),
            "different": "0" * 64,
        },
    )
    assert summary.reconciliation.archive_only == 1
    assert summary.reconciliation.database_only == 1
    assert summary.reconciliation.synchronized == 1
    assert summary.reconciliation.conflicts == 1


def test_summary_reports_conversation_model_route_accuracy_separately() -> None:
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    running = running_envelope(
        run_id="conversation-model-summary",
        evaluation_kind="conversation_model",
        scenario_id="conversation-model-suite",
        suite_version="conversation-model-v1",
        metadata={
            "workflowVersion": "conversation-model-v1",
            "scenarioVersion": "conversation-model-v1",
            "modelConfiguration": {"model": "fake"},
        },
        created_at=now,
        started_at=now,
    )
    completed = terminal_envelope(
        running=running,
        status="passed",
        validity="VALID_PASS",
        passed=True,
        metrics={"routeAccuracy": 1.0},
        result_payload={"failures": []},
        diagnostic_task_id=None,
        failure_category=None,
        completed_at=now,
    )

    summary = build_history_summary([completed], database_checksums={})

    assert "Conversation Route Accuracy 平均值：1.0000" in summary.markdown


def _investigation_run(
    run_id: str,
    *,
    strategy: str,
    root_cause: bool,
    recall: int,
    duration: int,
    model_calls: int,
    duplicates: int = 0,
    safe: bool = True,
) -> InvestigationBenchmarkMetrics:
    return InvestigationBenchmarkMetrics(
        strategy=strategy,
        effective_strategy=(
            "single_agent" if strategy == "single" else "multi_agent"
        ),
        policy_version="investigation-router-v1",
        root_cause_top1_correct=root_cause,
        evidence_recall_basis_points=recall,
        duration_ms=duration,
        model_call_count=model_calls,
        duplicate_evidence_basis_points=duplicates,
        fallback_reason=None,
        security_hard_gate_passed=safe,
        total_score=100 if root_cause else 80,
        run_id=run_id,
        scenario_id="APY-LIVE-PG-LOCK-001",
        campaign_id="strategy-ab-1",
    )


def test_investigation_strategy_gate_rejects_performance_without_capability_gain() -> None:
    single = [
        _investigation_run(
            f"single-{index}",
            strategy="single",
            root_cause=True,
            recall=9000,
            duration=1000,
            model_calls=3,
        )
        for index in range(3)
    ]
    multi = [
        _investigation_run(
            f"multi-{index}",
            strategy="multi",
            root_cause=True,
            recall=9000,
            duration=1200,
            model_calls=4,
        )
        for index in range(3)
    ]

    comparison = compare_investigation_strategies(single, multi)

    assert comparison.pair_count == 3
    assert comparison.performance_gate_passed is True
    assert comparison.capability_gate_passed is False
    assert comparison.eligibility == "benchmark_only"
    assert "capability_gain_missing" in comparison.reason_codes


def test_investigation_strategy_gate_accepts_safe_recall_gain() -> None:
    single = [
        _investigation_run(
            f"single-{index}",
            strategy="single",
            root_cause=False,
            recall=7000,
            duration=1000,
            model_calls=3,
        )
        for index in range(3)
    ]
    multi = [
        _investigation_run(
            f"multi-{index}",
            strategy="multi",
            root_cause=True,
            recall=8200,
            duration=1400,
            model_calls=5,
            duplicates=500,
        )
        for index in range(3)
    ]

    comparison = compare_investigation_strategies(single, multi)

    assert comparison.evidence_recall_gain_basis_points == 1200
    assert comparison.root_cause_top1_gain_basis_points == 10000
    assert comparison.performance_gate_passed is True
    assert comparison.capability_gate_passed is True
    assert comparison.security_gate_passed is True
    assert comparison.eligibility == "eligible_for_default_review"


def test_investigation_strategy_gate_rejects_any_security_failure() -> None:
    single = [
        _investigation_run(
            "single-safe",
            strategy="single",
            root_cause=False,
            recall=7000,
            duration=1000,
            model_calls=3,
        )
    ]
    multi = [
        _investigation_run(
            "multi-unsafe",
            strategy="multi",
            root_cause=True,
            recall=9000,
            duration=1200,
            model_calls=4,
            safe=False,
        )
    ]

    comparison = compare_investigation_strategies(single, multi)

    assert comparison.security_gate_passed is False
    assert comparison.eligibility == "benchmark_only"
    assert comparison.reason_codes[0] == "security_hard_gate_failed"


def test_investigation_strategy_gate_rejects_requested_multi_that_fell_back() -> None:
    single = _investigation_run(
        "single-effective",
        strategy="single",
        root_cause=True,
        recall=10000,
        duration=1000,
        model_calls=3,
    )
    requested_multi = _investigation_run(
        "multi-fell-back",
        strategy="multi",
        root_cause=True,
        recall=10000,
        duration=1000,
        model_calls=3,
    )
    requested_multi = replace(
        requested_multi,
        effective_strategy="single_agent",
    )

    with pytest.raises(ValueError, match="effective strategy"):
        compare_investigation_strategies([single], [requested_multi])
