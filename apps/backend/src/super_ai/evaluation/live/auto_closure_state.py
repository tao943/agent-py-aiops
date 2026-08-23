"""PostgreSQL-backed optimistic state for resumable Live auto-closure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.evaluation.live.domain import LiveCheck, LiveFaultObservation
from super_ai.memory.models import LiveAutoClosureStateModel

AutoClosureStage = Literal[
    "baseline_ready",
    "fault_injected",
    "alert_detected",
    "diagnosis_completed",
    "recovery_dispatched",
    "recovery_completed",
    "verification_recorded",
    "resolved",
]

_STAGE_ORDER: tuple[AutoClosureStage, ...] = (
    "baseline_ready",
    "fault_injected",
    "alert_detected",
    "diagnosis_completed",
    "recovery_dispatched",
    "recovery_completed",
    "verification_recorded",
    "resolved",
)


def stage_at_least(current: AutoClosureStage, requested: AutoClosureStage) -> bool:
    return _STAGE_ORDER.index(current) >= _STAGE_ORDER.index(requested)


@dataclass(frozen=True, slots=True)
class AutoClosureState:
    stage: AutoClosureStage
    driver_state: dict[str, object]
    observation: LiveFaultObservation | None = None
    correlation: dict[str, str | None] | None = None
    recovery_execution_key: str | None = None
    recovery_payload: dict[str, object] | None = None
    verification_payload: dict[str, object] | None = None
    version: int = 0


class AutoClosureStateConflict(RuntimeError):
    pass


class SQLAlchemyAutoClosureStateRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
        driver_state: dict[str, object],
    ) -> AutoClosureState:
        now = datetime.now(timezone.utc)
        state = AutoClosureState(
            stage="baseline_ready",
            driver_state=dict(driver_state),
        )
        values = {
            "owner_user_id": owner_user_id,
            "source_id": source_id,
            "scenario_id": scenario_id,
            "run_id": run_id,
            "stage": state.stage,
            "state_payload": _state_payload(state),
            "version": 0,
            "created_at": now,
            "updated_at": now,
        }
        async with self._session_factory() as session:
            await session.execute(
                postgresql_insert(LiveAutoClosureStateModel)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "owner_user_id",
                        "source_id",
                        "scenario_id",
                        "run_id",
                    ]
                )
            )
            await session.commit()
        stored = await self.load(
            owner_user_id=owner_user_id,
            source_id=source_id,
            scenario_id=scenario_id,
            run_id=run_id,
        )
        if stored is None:
            raise AutoClosureStateConflict("auto_closure_state_missing_after_create")
        if stored.driver_state != state.driver_state:
            raise AutoClosureStateConflict("auto_closure_baseline_conflict")
        return stored

    async def load(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
    ) -> AutoClosureState | None:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(LiveAutoClosureStateModel).where(
                        LiveAutoClosureStateModel.owner_user_id == owner_user_id,
                        LiveAutoClosureStateModel.source_id == source_id,
                        LiveAutoClosureStateModel.scenario_id == scenario_id,
                        LiveAutoClosureStateModel.run_id == run_id,
                    )
                )
            ).one_or_none()
        return _state_from_row(row) if row is not None else None

    async def save(
        self,
        *,
        owner_user_id: str,
        source_id: str,
        scenario_id: str,
        run_id: str,
        state: AutoClosureState,
    ) -> AutoClosureState:
        if state.version < 0:
            raise AutoClosureStateConflict("auto_closure_state_version_invalid")
        existing = await self.load(
            owner_user_id=owner_user_id,
            source_id=source_id,
            scenario_id=scenario_id,
            run_id=run_id,
        )
        if existing is None:
            raise AutoClosureStateConflict("auto_closure_state_missing")
        if _STAGE_ORDER.index(state.stage) < _STAGE_ORDER.index(existing.stage):
            raise AutoClosureStateConflict("auto_closure_stage_regression")
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    update(LiveAutoClosureStateModel)
                    .where(
                        LiveAutoClosureStateModel.owner_user_id == owner_user_id,
                        LiveAutoClosureStateModel.source_id == source_id,
                        LiveAutoClosureStateModel.scenario_id == scenario_id,
                        LiveAutoClosureStateModel.run_id == run_id,
                        LiveAutoClosureStateModel.version == state.version,
                    )
                    .values(
                        stage=state.stage,
                        state_payload=_state_payload(state),
                        version=state.version + 1,
                        updated_at=now,
                    )
                    .returning(LiveAutoClosureStateModel)
                )
            ).scalar_one_or_none()
            await session.commit()
        if row is None:
            raise AutoClosureStateConflict("auto_closure_stale_state")
        return _state_from_row(row)


def _state_payload(state: AutoClosureState) -> dict[str, object]:
    return {
        "driverState": dict(state.driver_state),
        "observation": _observation_payload(state.observation),
        "correlation": dict(state.correlation) if state.correlation else None,
        "recoveryExecutionKey": state.recovery_execution_key,
        "recovery": dict(state.recovery_payload) if state.recovery_payload else None,
        "verification": (
            dict(state.verification_payload) if state.verification_payload else None
        ),
    }


def _state_from_row(row: LiveAutoClosureStateModel) -> AutoClosureState:
    payload = cast(dict[str, object], row.state_payload)
    driver_state = payload.get("driverState")
    if not isinstance(driver_state, dict):
        raise AutoClosureStateConflict("auto_closure_driver_state_invalid")
    stage = row.stage
    if stage not in _STAGE_ORDER:
        raise AutoClosureStateConflict("auto_closure_stage_invalid")
    return AutoClosureState(
        stage=stage,
        driver_state=cast(dict[str, object], driver_state),
        observation=_observation_from_payload(payload.get("observation")),
        correlation=_optional_correlation(payload.get("correlation")),
        recovery_execution_key=_optional_text(payload.get("recoveryExecutionKey")),
        recovery_payload=_optional_dict(payload.get("recovery")),
        verification_payload=_optional_dict(payload.get("verification")),
        version=row.version,
    )


def _observation_payload(
    observation: LiveFaultObservation | None,
) -> dict[str, object] | None:
    if observation is None:
        return None
    return {
        "scenarioId": observation.scenario_id,
        "checks": [
            {"name": check.name, "passed": check.passed}
            for check in observation.checks
        ],
        "safeFacts": [[key, value] for key, value in observation.safe_facts],
    }


def _observation_from_payload(value: object) -> LiveFaultObservation | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AutoClosureStateConflict("auto_closure_observation_invalid")
    payload = cast(dict[str, object], value)
    scenario_id = payload.get("scenarioId")
    checks = payload.get("checks")
    facts = payload.get("safeFacts")
    if (
        not isinstance(scenario_id, str)
        or not isinstance(checks, list)
        or not isinstance(facts, list)
    ):
        raise AutoClosureStateConflict("auto_closure_observation_invalid")
    parsed_checks: list[LiveCheck] = []
    for raw_check in cast(list[object], checks):
        if not isinstance(raw_check, dict):
            raise AutoClosureStateConflict("auto_closure_observation_invalid")
        check = cast(dict[str, object], raw_check)
        name = check.get("name")
        passed = check.get("passed")
        if not isinstance(name, str) or not isinstance(passed, bool):
            raise AutoClosureStateConflict("auto_closure_observation_invalid")
        parsed_checks.append(LiveCheck(name, passed))

    parsed_facts: list[tuple[str, str | int | float | bool]] = []
    for raw_fact in cast(list[object], facts):
        if not isinstance(raw_fact, list):
            raise AutoClosureStateConflict("auto_closure_observation_invalid")
        fact = cast(list[object], raw_fact)
        if len(fact) != 2:
            raise AutoClosureStateConflict("auto_closure_observation_invalid")
        key, fact_value = fact
        if not isinstance(key, str) or not isinstance(
            fact_value, (str, int, float, bool)
        ):
            raise AutoClosureStateConflict("auto_closure_observation_invalid")
        parsed_facts.append((key, fact_value))
    return LiveFaultObservation(
        scenario_id,
        tuple(parsed_checks),
        tuple(parsed_facts),
    )


def _optional_dict(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AutoClosureStateConflict("auto_closure_state_payload_invalid")
    return cast(dict[str, object], value)


def _optional_correlation(value: object) -> dict[str, str | None] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AutoClosureStateConflict("auto_closure_correlation_invalid")
    correlation = cast(dict[object, object], value)
    if any(
        not isinstance(key, str) or item is not None and not isinstance(item, str)
        for key, item in correlation.items()
    ):
        raise AutoClosureStateConflict("auto_closure_correlation_invalid")
    return cast(dict[str, str | None], value)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AutoClosureStateConflict("auto_closure_state_payload_invalid")
    return value
