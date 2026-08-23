from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from super_ai.evaluation.live.auto_closure_state import (
    AutoClosureStateConflict,
    SQLAlchemyAutoClosureStateRepository,
)
from super_ai.evaluation.live.domain import LiveCheck, LiveFaultObservation
from super_ai.memory.database import create_memory_engine, create_memory_session_factory

SCENARIO_ID = "APY-LIVE-ORDER-POOL-LEAK-001"


def _observation() -> LiveFaultObservation:
    return LiveFaultObservation(
        scenario_id=SCENARIO_ID,
        checks=(LiveCheck("pool_at_capacity", True),),
        safe_facts=(("poolCapacity", 3),),
    )


@pytest.mark.asyncio
async def test_auto_closure_state_round_trips_and_rejects_stale_updates(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = SQLAlchemyAutoClosureStateRepository(
        create_memory_session_factory(engine)
    )
    run_id = f"auto-state-{uuid4().hex}"
    identity = {
        "owner_user_id": "benchmark-user",
        "source_id": "local-alertmanager",
        "scenario_id": SCENARIO_ID,
        "run_id": run_id,
    }
    try:
        created = await repository.create(
            **identity,
            driver_state={
                "originalGeneration": "generation-1",
                "unrelatedSessionFingerprints": ["a" * 64],
            },
        )
        duplicate = await repository.create(
            **identity,
            driver_state=created.driver_state,
        )
        updated = await repository.save(
            **identity,
            state=replace(
                created,
                stage="fault_injected",
                observation=_observation(),
            ),
        )
        loaded = await repository.load(**identity)
        cross_tenant = await repository.load(
            **{**identity, "owner_user_id": "another-user"}
        )
        with pytest.raises(AutoClosureStateConflict, match="stale"):
            await repository.save(
                **identity,
                state=replace(created, stage="fault_injected"),
            )
    finally:
        await engine.dispose()

    assert created.stage == "baseline_ready"
    assert duplicate == created
    assert updated.version == 1
    assert loaded == updated
    assert loaded is not None and loaded.observation == _observation()
    assert cross_tenant is None


@pytest.mark.asyncio
async def test_auto_closure_state_create_rejects_changed_baseline(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    repository = SQLAlchemyAutoClosureStateRepository(
        create_memory_session_factory(engine)
    )
    identity = {
        "owner_user_id": "benchmark-user",
        "source_id": "local-alertmanager",
        "scenario_id": SCENARIO_ID,
        "run_id": f"auto-state-{uuid4().hex}",
    }
    try:
        await repository.create(**identity, driver_state={"originalGeneration": "one"})
        with pytest.raises(AutoClosureStateConflict, match="baseline"):
            await repository.create(
                **identity,
                driver_state={"originalGeneration": "two"},
            )
    finally:
        await engine.dispose()
