from datetime import datetime, timezone

from super_ai.evaluation.history import (
    EvaluationKind,
    EvaluationProvenance,
    EvaluationRunEnvelope,
    artifact_checksum,
    running_envelope,
    terminal_envelope,
)
from super_ai.evaluation.summary import build_history_summary


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
