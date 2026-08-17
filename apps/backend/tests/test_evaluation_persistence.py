from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import StatementError

from super_ai.evaluation.history import artifact_checksum, running_envelope, terminal_envelope
from super_ai.evaluation.persistence import EvaluationRepository
from super_ai.evaluation.scoring import EvaluationResult, ScoreReason
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.sqlalchemy import (
    SQLAlchemyDiagnosticMemoryRepository,
    SQLAlchemyEvaluationRepository,
)


def passing_result() -> EvaluationResult:
    return EvaluationResult(
        outcome=20,
        diagnosis=25,
        evidence=20,
        process=15,
        safety=15,
        efficiency=5,
        raw_total=100,
        total=100,
        validity="valid",
        passed=True,
        failures=(),
        hard_gate=None,
        reasons=(ScoreReason("primary_mechanism_correct", 10, 10, ("ev-1",)),),
    )


def retrieval_envelopes():
    started_at = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
    running = running_envelope(
        run_id="eval-retrieval-generic",
        evaluation_kind="retrieval",
        scenario_id="retrieval-64",
        suite_version="v1",
        metadata={
            "gitSha": "abc123",
            "workflowVersion": "retrieval-v1",
            "modelConfiguration": {
                "embeddingModel": "text-embedding-v4",
                "rerankModel": "qwen3-vl-rerank",
            },
            "datasetChecksum": "d" * 64,
            "ownerUserId": "eval-owner",
            "knowledgeBaseId": "kb-eval-owner",
        },
        created_at=started_at,
        started_at=started_at,
    )
    terminal = terminal_envelope(
        running=running,
        status="failed",
        validity="VALID_FAIL",
        passed=False,
        metrics={
            "queryCount": 64,
            "answerableQueryCount": 58,
            "noAnswerProbeCount": 6,
            "recallAt1": 0.79,
            "recallAt3": 0.95,
            "mrr": 0.85,
            "forbiddenTopOneRate": 0.0,
            "citationCompletenessRate": 1.0,
            "vectorChannelCoverageRate": 1.0,
            "bm25ChannelCoverageRate": 1.0,
            "hybridChannelCoverageRate": 1.0,
        },
        result_payload={"failures": ["recall_at_1_below_threshold"]},
        diagnostic_task_id=None,
        failure_category=None,
        completed_at=started_at,
    )
    return running, terminal


@pytest.mark.asyncio
async def test_generic_retrieval_envelope_round_trips(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    running, terminal = retrieval_envelopes()
    checksum = artifact_checksum(terminal)
    try:
        started = await repository.start_envelope(running)
        finalized = await repository.finalize_envelope(
            terminal,
            artifact_checksum=checksum,
        )
        loaded = await repository.get_run_with_result(running.run_id)
    finally:
        await engine.dispose()

    assert started.status == "running"
    assert started.evaluation_kind == "retrieval"
    assert started.run_metadata == running.metadata
    assert finalized[0].status == "failed"
    assert finalized[0].artifact_checksum == checksum
    assert finalized[1] is not None
    assert finalized[1].total is None
    assert finalized[1].metrics["recallAt1"] == 0.79
    assert finalized[1].result_payload == terminal.result_payload
    assert loaded == finalized


@pytest.mark.asyncio
async def test_concurrent_generic_start_is_idempotent(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    first_repository = EvaluationRepository(session_factory)
    second_repository = EvaluationRepository(session_factory)
    running, _terminal = retrieval_envelopes()
    try:
        first, second = await asyncio.gather(
            first_repository.start_envelope(running),
            second_repository.start_envelope(running),
        )
    finally:
        await engine.dispose()
    assert first == second


@pytest.mark.asyncio
async def test_generic_finalize_rejects_different_checksum(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    running, terminal = retrieval_envelopes()
    try:
        await repository.start_envelope(running)
        await repository.finalize_envelope(
            terminal,
            artifact_checksum=artifact_checksum(terminal),
        )
        with pytest.raises(ValueError, match="different evaluation result"):
            await repository.finalize_envelope(terminal, artifact_checksum="0" * 64)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_attach_artifact_checksum_only_fills_null_or_same_value(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    running, _terminal = retrieval_envelopes()
    try:
        await repository.start_envelope(running)
        filled = await repository.attach_artifact_checksum(
            run_id=running.run_id,
            artifact_checksum="a" * 64,
        )
        repeated = await repository.attach_artifact_checksum(
            run_id=running.run_id,
            artifact_checksum="a" * 64,
        )
        with pytest.raises(ValueError, match="different artifact checksum"):
            await repository.attach_artifact_checksum(
                run_id=running.run_id,
                artifact_checksum="b" * 64,
            )
    finally:
        await engine.dispose()
    assert filled == repeated
    assert filled.artifact_checksum == "a" * 64


@pytest.mark.asyncio
async def test_administrative_history_queries_are_complete_and_benchmark_scoped(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    repository = EvaluationRepository(session_factory)
    diagnostics = SQLAlchemyDiagnosticMemoryRepository(session_factory)
    running, terminal = retrieval_envelopes()
    try:
        await repository.start_envelope(running)
        finalized = await repository.finalize_envelope(
            terminal, artifact_checksum=artifact_checksum(terminal)
        )
        benchmark_task = await diagnostics.create_task(
            owner_user_id="eval-owner",
            task_id="benchmark-task",
            status="completed",
            query="benchmark",
            input_payload={"benchmarkMode": "live"},
        )
        await diagnostics.create_task(
            owner_user_id="eval-owner",
            task_id="ordinary-task",
            status="completed",
            query="ordinary",
            input_payload={},
        )

        runs = await repository.list_runs_with_results()
        tasks = await repository.list_benchmark_diagnostic_tasks()
    finally:
        await engine.dispose()

    assert finalized in runs
    assert benchmark_task in tasks
    assert all(task.input_payload.get("benchmarkMode") in {"snapshot", "live"} for task in tasks)


@pytest.mark.asyncio
async def test_create_run_is_idempotent_but_rejects_identity_changes(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    created_at = datetime(2026, 8, 10, tzinfo=timezone.utc)
    try:
        first = await repository.create_run(
            run_id="run-idempotent",
            scenario_id="APY-003",
            mode="snapshot",
            suite_version="v1",
            agent_version={"git_sha": "abc123"},
            model_configuration={"provider": "dashscope", "model": "qwen-plus"},
            created_at=created_at,
        )
        second = await repository.create_run(
            run_id="run-idempotent",
            scenario_id="APY-003",
            mode="snapshot",
            suite_version="v1",
            agent_version={"git_sha": "abc123"},
            model_configuration={"provider": "dashscope", "model": "qwen-plus"},
            created_at=created_at,
        )

        with pytest.raises(ValueError, match="different evaluation identity"):
            await repository.create_run(
                run_id="run-idempotent",
                scenario_id="APY-006",
                mode="snapshot",
                suite_version="v1",
                agent_version={"git_sha": "different"},
                model_configuration={"provider": "dashscope", "model": "qwen-plus"},
            )
    finally:
        await engine.dispose()

    assert first == second
    assert first.status == "pending"


@pytest.mark.asyncio
async def test_concurrent_create_run_with_same_identity_is_idempotent(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    first_repository = EvaluationRepository(session_factory)
    second_repository = EvaluationRepository(session_factory)
    try:
        first, second = await asyncio.gather(
            first_repository.create_run(
                run_id="run-concurrent-same",
                scenario_id="APY-003",
                mode="snapshot",
                suite_version="v1",
                agent_version={"git_sha": "abc123"},
                model_configuration={"provider": "offline", "model": "scripted"},
            ),
            second_repository.create_run(
                run_id="run-concurrent-same",
                scenario_id="APY-003",
                mode="snapshot",
                suite_version="v1",
                agent_version={"git_sha": "abc123"},
                model_configuration={"provider": "offline", "model": "scripted"},
            ),
        )
    finally:
        await engine.dispose()

    assert first == second
    assert first.status == "pending"


@pytest.mark.asyncio
async def test_concurrent_create_run_rejects_different_identity_without_poisoning_session(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    first_repository = EvaluationRepository(session_factory)
    second_repository = EvaluationRepository(session_factory)
    try:
        outcomes = await asyncio.gather(
            first_repository.create_run(
                run_id="run-concurrent-conflict",
                scenario_id="APY-003",
                mode="snapshot",
                suite_version="v1",
                agent_version={"git_sha": "abc123"},
                model_configuration={"provider": "offline", "model": "scripted"},
            ),
            second_repository.create_run(
                run_id="run-concurrent-conflict",
                scenario_id="APY-006",
                mode="snapshot",
                suite_version="v1",
                agent_version={"git_sha": "abc123"},
                model_configuration={"provider": "offline", "model": "scripted"},
            ),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if not isinstance(item, BaseException)]
        failures = [item for item in outcomes if isinstance(item, BaseException)]

        recovered = await second_repository.create_run(
            run_id="run-after-conflict",
            scenario_id="APY-003",
            mode="snapshot",
            suite_version="v1",
            agent_version={"git_sha": "abc123"},
            model_configuration={"provider": "offline", "model": "scripted"},
        )
        loaded = await second_repository.get_run_with_result("run-after-conflict")
    finally:
        await engine.dispose()

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert str(failures[0]) == (
        "Run run-concurrent-conflict has a different evaluation identity."
    )
    assert loaded == (recovered, None)


@pytest.mark.asyncio
async def test_result_requires_completed_run_and_round_trips_scorecard(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    try:
        await repository.create_run(
            run_id="run-score",
            scenario_id="APY-003",
            mode="snapshot",
            suite_version="v1",
            agent_version={"git_sha": "abc123"},
            model_configuration={"provider": "offline", "model": "scripted"},
        )

        with pytest.raises(ValueError, match="must be completed"):
            await repository.save_result(
                result_id="result-score",
                run_id="run-score",
                result=passing_result(),
            )

        completed = await repository.complete_run(
            run_id="run-score",
            diagnostic_task_id=None,
        )
        saved = await repository.save_result(
            result_id="result-score",
            run_id="run-score",
            result=passing_result(),
        )
        loaded = await repository.get_run_with_result("run-score")
    finally:
        await engine.dispose()

    assert completed.status == "completed"
    assert completed.started_at is not None
    assert completed.completed_at is not None
    assert saved.dimension_scores == {
        "outcome": 20,
        "diagnosis": 25,
        "evidence": 20,
        "process": 15,
        "safety": 15,
        "efficiency": 5,
    }
    assert loaded is not None
    assert loaded[1] is not None
    assert loaded == (completed, saved)
    assert loaded[1].score_reasons[0]["evidence_ids"] == ["ev-1"]


@pytest.mark.asyncio
async def test_finalize_run_is_atomic_and_idempotent(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    try:
        await repository.create_run(
            run_id="run-finalize",
            scenario_id="APY-003",
            mode="snapshot",
            suite_version="v1",
            agent_version={"git_sha": "abc123"},
            model_configuration={"provider": "offline", "model": "scripted"},
        )

        finalized = await repository.finalize_run(
            run_id="run-finalize",
            result_id="result-finalize",
            result=passing_result(),
            diagnostic_task_id=None,
        )
        repeated = await repository.finalize_run(
            run_id="run-finalize",
            result_id="result-finalize",
            result=passing_result(),
            diagnostic_task_id=None,
        )
    finally:
        await engine.dispose()

    assert repeated == finalized
    assert finalized[0].status == "completed"
    assert finalized[1].result_id == "result-finalize"


@pytest.mark.asyncio
async def test_finalize_run_rejects_a_different_scorecard(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    try:
        await repository.create_run(
            run_id="run-finalize-conflict",
            scenario_id="APY-003",
            mode="snapshot",
            suite_version="v1",
            agent_version={"git_sha": "abc123"},
            model_configuration={"provider": "offline", "model": "scripted"},
        )
        await repository.finalize_run(
            run_id="run-finalize-conflict",
            result_id="result-original",
            result=passing_result(),
            diagnostic_task_id=None,
        )

        with pytest.raises(ValueError, match="different scorecard"):
            await repository.finalize_run(
                run_id="run-finalize-conflict",
                result_id="result-different",
                result=passing_result(),
                diagnostic_task_id=None,
            )

        changed = passing_result()
        changed = EvaluationResult(
            outcome=changed.outcome,
            diagnosis=changed.diagnosis,
            evidence=changed.evidence,
            process=changed.process,
            safety=changed.safety,
            efficiency=changed.efficiency,
            raw_total=changed.raw_total,
            total=99,
            validity=changed.validity,
            passed=changed.passed,
            failures=changed.failures,
            hard_gate=changed.hard_gate,
            reasons=changed.reasons,
        )
        with pytest.raises(ValueError, match="different scorecard"):
            await repository.finalize_run(
                run_id="run-finalize-conflict",
                result_id="result-original",
                result=changed,
                diagnostic_task_id=None,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalize_run_rolls_back_status_when_scorecard_serialization_fails(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    repository = EvaluationRepository(session_factory)
    raw_repository = SQLAlchemyEvaluationRepository(session_factory)
    try:
        pending = await repository.create_run(
            run_id="run-finalize-rollback",
            scenario_id="APY-003",
            mode="snapshot",
            suite_version="v1",
            agent_version={"git_sha": "abc123"},
            model_configuration={"provider": "offline", "model": "scripted"},
        )

        with pytest.raises(StatementError):
            await raw_repository.finalize_run(
                run_id="run-finalize-rollback",
                result_id="result-finalize-rollback",
                dimension_scores={"outcome": 20},
                total=100,
                raw_total=100,
                validity="valid",
                passed=True,
                failures=[],
                score_reasons=[{"reason": object()}],
                hard_gate=None,
                diagnostic_task_id=None,
            )

        loaded = await repository.get_run_with_result("run-finalize-rollback")
    finally:
        await engine.dispose()

    assert loaded == (pending, None)


@pytest.mark.asyncio
async def test_model_configuration_rejects_secret_material(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    try:
        with pytest.raises(ValueError, match="must not contain secrets"):
            await repository.create_run(
                run_id="run-secret",
                scenario_id="APY-003",
                mode="snapshot",
                suite_version="v1",
                agent_version={"git_sha": "abc123"},
                model_configuration={"provider": "dashscope", "api_key": "do-not-store"},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_run_persists_only_safe_terminal_metadata(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = EvaluationRepository(create_memory_session_factory(engine))
    try:
        await repository.create_run(
            run_id="run-failed",
            scenario_id="APY-003",
            mode="snapshot",
            suite_version="v1",
            agent_version={"git_sha": "abc123"},
            model_configuration={"provider": "offline", "model": "scripted"},
        )

        failed = await repository.fail_run(
            run_id="run-failed",
            status="agent_failed",
            failure_category="adapter_error",
        )
        repeated = await repository.fail_run(
            run_id="run-failed",
            status="agent_failed",
            failure_category="adapter_error",
        )

        with pytest.raises(ValueError, match="failure category"):
            await repository.fail_run(
                run_id="run-failed",
                status="agent_failed",
                failure_category="secret=must-not-persist",
            )
        with pytest.raises(ValueError, match="failure status"):
            await repository.fail_run(
                run_id="run-failed",
                status="pending",  # type: ignore[arg-type]
                failure_category="adapter_error",
            )
        with pytest.raises(ValueError, match="terminal state"):
            await repository.fail_run(
                run_id="run-failed",
                status="infra_failed",
                failure_category="persistence_error",
            )
    finally:
        await engine.dispose()

    assert failed == repeated
    assert failed.status == "agent_failed"
    assert failed.failure_category == "adapter_error"
    assert "must-not-persist" not in repr(failed)
