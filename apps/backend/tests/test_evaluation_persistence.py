from __future__ import annotations

from datetime import datetime, timezone

import pytest

from super_ai.evaluation.persistence import EvaluationRepository
from super_ai.evaluation.scoring import EvaluationResult, ScoreReason
from super_ai.memory.database import create_memory_engine, create_memory_session_factory


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
