"""Fresh-probed, approval-bound PostgreSQL blocker recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.alert_ingestion.repositories import AlertIncidentRecord
from super_ai.recovery.config import PostgresLockResource, PostgresRecoveryTarget
from super_ai.recovery.contracts import (
    RecoveryCheck,
    RecoveryExecutionResult,
    RecoveryVerificationResult,
)
from super_ai.recovery.proposal_adapter import lock_relationship_fingerprint
from super_ai.recovery.repository import RecoveryApprovalRecord


@dataclass(frozen=True, slots=True)
class PostgresBlockerRelation:
    database_identity: str
    logical_resource: str
    blocker_pid: int
    waiter_pid: int
    observer_pid: int
    blocker_backend_type: str
    waiter_backend_type: str
    blocker_application_name: str
    waiter_application_name: str


@dataclass(frozen=True, slots=True)
class PostgresProbeSnapshot:
    database_identity: str
    observer_pid: int
    relations: tuple[PostgresBlockerRelation, ...]


@dataclass(frozen=True, slots=True)
class PostgresPreflightExpectation:
    owner_user_id: str
    incident_id: str
    intent_id: str
    proposal_fingerprint: str
    relationship_fingerprint: str
    logical_resource: str


@dataclass(frozen=True, slots=True)
class PostgresPreflightResult:
    allowed: bool
    relation: PostgresBlockerRelation | None
    relationship_fingerprint: str | None
    safe_reason_code: str | None


@dataclass(frozen=True, slots=True)
class PostgresTerminationResult:
    terminated: bool
    outcome_known: bool
    duration_ms: int


@dataclass(frozen=True, slots=True)
class PostgresVerificationFacts:
    blocker_exists: bool
    waiter_progressed: bool
    lock_wait_present: bool


class PostgresRecoveryAdapter(Protocol):
    async def probe(self, resource: PostgresLockResource) -> PostgresProbeSnapshot: ...

    async def terminate(self, pid: int) -> PostgresTerminationResult: ...

    async def verify(
        self,
        relation: PostgresBlockerRelation,
        resource: PostgresLockResource,
    ) -> PostgresVerificationFacts: ...


class IncidentStatusReader(Protocol):
    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None: ...


class PostgresRecoveryExecutor:
    def __init__(
        self,
        *,
        target: PostgresRecoveryTarget,
        adapter: PostgresRecoveryAdapter,
        incidents: IncidentStatusReader,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = target
        self._adapter = adapter
        self._incidents = incidents
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def preflight(
        self,
        expectation: PostgresPreflightExpectation,
    ) -> PostgresPreflightResult:
        resource = self._target.lock_resource_mappings.get(
            expectation.logical_resource
        )
        if resource is None:
            return _preflight_denied("postgres_lock_relationship_changed")
        try:
            snapshot = await self._adapter.probe(resource)
        except (OSError, RuntimeError, ValueError, SQLAlchemyError):
            return _preflight_denied("postgres_probe_unavailable")
        if snapshot.database_identity != self._target.database_identity:
            return _preflight_denied("postgres_database_changed")
        if len(snapshot.relations) != 1:
            return _preflight_denied("postgres_blocker_not_unique")
        relation = snapshot.relations[0]
        if relation.database_identity != snapshot.database_identity:
            return _preflight_denied("postgres_database_changed")
        if relation.logical_resource != expectation.logical_resource:
            return _preflight_denied("postgres_lock_relationship_changed")
        if relation.blocker_backend_type != "client backend":
            return _preflight_denied("postgres_blocker_not_client")
        if relation.waiter_backend_type != "client backend":
            return _preflight_denied("postgres_waiter_not_client")
        if relation.blocker_pid == relation.waiter_pid:
            return _preflight_denied("postgres_waiter_targeted")
        if (
            relation.blocker_pid == snapshot.observer_pid
            or relation.blocker_pid == relation.observer_pid
            or relation.blocker_application_name.startswith("agentpy-recovery:")
        ):
            return _preflight_denied("postgres_recovery_connection_targeted")
        if relation.blocker_pid <= 0 or relation.waiter_pid <= 0:
            return _preflight_denied("postgres_lock_relationship_changed")
        fingerprint = _fresh_relationship_fingerprint(self._target, resource)
        if fingerprint != expectation.relationship_fingerprint:
            return _preflight_denied("postgres_lock_relationship_changed")
        return PostgresPreflightResult(True, relation, fingerprint, None)

    async def execute_once(
        self,
        expectation: PostgresPreflightExpectation,
        preflight: PostgresPreflightResult,
        approval: RecoveryApprovalRecord | None,
    ) -> RecoveryExecutionResult:
        approval_reason = _approval_error(approval, expectation, self._now())
        if approval_reason is not None:
            return RecoveryExecutionResult(False, True, approval_reason, 0)
        if not preflight.allowed or preflight.relation is None:
            return RecoveryExecutionResult(
                False,
                True,
                preflight.safe_reason_code or "postgres_preflight_invalid",
                0,
            )
        fresh = await self.preflight(expectation)
        if (
            not fresh.allowed
            or fresh.relation is None
            or not _same_lock_relation(fresh.relation, preflight.relation)
            or fresh.relationship_fingerprint != preflight.relationship_fingerprint
        ):
            return RecoveryExecutionResult(
                False,
                True,
                "postgres_lock_relationship_changed",
                0,
            )
        result = await self._adapter.terminate(fresh.relation.blocker_pid)
        if not result.outcome_known:
            return RecoveryExecutionResult(
                False,
                False,
                "postgres_termination_outcome_unknown",
                result.duration_ms,
            )
        return RecoveryExecutionResult(
            result.terminated,
            True,
            (
                "postgres_blocker_terminated"
                if result.terminated
                else "postgres_termination_failed"
            ),
            result.duration_ms,
        )

    async def verify(
        self,
        expectation: PostgresPreflightExpectation,
        relation: PostgresBlockerRelation,
    ) -> RecoveryVerificationResult:
        resource = self._target.lock_resource_mappings.get(
            expectation.logical_resource
        )
        facts = (
            await self._safe_verify(relation, resource)
            if resource is not None
            else PostgresVerificationFacts(True, False, True)
        )
        incident = await self._safe_incident(
            expectation.owner_user_id,
            expectation.incident_id,
        )
        checks_values = (
            ("blocker_gone", not facts.blocker_exists),
            ("waiter_progressed", facts.waiter_progressed),
            ("lock_wait_recovered", not facts.lock_wait_present),
            (
                "incident_resolved",
                incident is not None and incident.status == "resolved",
            ),
        )
        now = self._now()
        checks = tuple(
            RecoveryCheck(
                key,
                "passed" if passed else "failed",
                f"{key} passed." if passed else f"{key} failed.",
                now,
            )
            for key, passed in checks_values
        )
        passed = all(check.status == "passed" for check in checks)
        return RecoveryVerificationResult(
            passed,
            checks,
            "postgres_recovery_verified" if passed else "postgres_verification_failed",
        )

    async def _safe_verify(
        self,
        relation: PostgresBlockerRelation,
        resource: PostgresLockResource,
    ) -> PostgresVerificationFacts:
        try:
            return await self._adapter.verify(relation, resource)
        except (OSError, RuntimeError, ValueError, SQLAlchemyError):
            return PostgresVerificationFacts(True, False, True)

    async def _safe_incident(
        self, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        try:
            return await self._incidents.get_owned(
                owner_user_id=owner_user_id,
                incident_id=incident_id,
            )
        except (OSError, RuntimeError, ValueError, SQLAlchemyError):
            return None


class SQLAlchemyPostgresRecoveryAdapter:
    """Use fixed parameterized SQL against one named server-side database target."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def probe(self, resource: PostgresLockResource) -> PostgresProbeSnapshot:
        async with self._session_factory() as session:
            identity_row = (
                await session.execute(
                    text(
                        "SELECT current_database() AS database_identity, "
                        "pg_backend_pid() AS observer_pid"
                    )
                )
            ).mappings().one()
            rows = (
                await session.execute(
                    text(_BLOCKER_QUERY),
                    {"schema": resource.schema, "relation": resource.relation},
                )
            ).mappings().all()
        database_identity = str(identity_row["database_identity"])
        observer_pid = int(identity_row["observer_pid"])
        relations = tuple(
            PostgresBlockerRelation(
                database_identity=database_identity,
                logical_resource=resource.logical_resource,
                blocker_pid=int(row["blocker_pid"]),
                waiter_pid=int(row["waiter_pid"]),
                observer_pid=observer_pid,
                blocker_backend_type=str(row["blocker_backend_type"]),
                waiter_backend_type=str(row["waiter_backend_type"]),
                blocker_application_name=str(row["blocker_application_name"] or ""),
                waiter_application_name=str(row["waiter_application_name"] or ""),
            )
            for row in rows
        )
        return PostgresProbeSnapshot(database_identity, observer_pid, relations)

    async def terminate(self, pid: int) -> PostgresTerminationResult:
        started_at = monotonic()
        try:
            async with self._session_factory() as session:
                value = (
                    await session.execute(
                        text("SELECT pg_terminate_backend(:pid)"),
                        {"pid": pid},
                    )
                ).scalar_one_or_none()
            if not isinstance(value, bool):
                return PostgresTerminationResult(False, True, _duration_ms(started_at))
            return PostgresTerminationResult(value, True, _duration_ms(started_at))
        except SQLAlchemyError:
            return PostgresTerminationResult(False, False, _duration_ms(started_at))

    async def verify(
        self,
        relation: PostgresBlockerRelation,
        resource: PostgresLockResource,
    ) -> PostgresVerificationFacts:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(_VERIFY_QUERY),
                    {
                        "blocker_pid": relation.blocker_pid,
                        "waiter_pid": relation.waiter_pid,
                        "schema": resource.schema,
                        "relation": resource.relation,
                    },
                )
            ).mappings().one()
        return PostgresVerificationFacts(
            blocker_exists=bool(row["blocker_exists"]),
            waiter_progressed=not bool(row["waiter_waiting"]),
            lock_wait_present=bool(row["waiter_waiting"]),
        )


def _fresh_relationship_fingerprint(
    target: PostgresRecoveryTarget,
    resource: PostgresLockResource,
) -> str:
    return lock_relationship_fingerprint(
        target_key=target.target_key,
        database_config_key=target.database_config_key,
        database_identity=target.database_identity,
        component="postgresql",
        mechanism="row_lock_blocking",
        required_values={
            "InspectPostgresLockGraph.blockerEdgeConfirmed": True,
            "InspectPostgresLockGraph.blockerRole": "transaction",
            "InspectPostgresLockGraph.lockedResource": resource.logical_resource,
        },
    )


def _approval_error(
    approval: RecoveryApprovalRecord | None,
    expectation: PostgresPreflightExpectation,
    now: datetime,
) -> str | None:
    if approval is None or approval.decision != "approved":
        return "postgres_approval_required"
    if approval.expires_at <= now:
        return "postgres_approval_expired"
    if (
        approval.owner_user_id != expectation.owner_user_id
        or approval.approver_user_id != expectation.owner_user_id
        or approval.incident_id != expectation.incident_id
        or approval.intent_id != expectation.intent_id
        or approval.proposal_fingerprint != expectation.proposal_fingerprint
    ):
        return "postgres_approval_mismatch"
    return None


def _preflight_denied(reason: str) -> PostgresPreflightResult:
    return PostgresPreflightResult(False, None, None, reason)


def _same_lock_relation(
    left: PostgresBlockerRelation,
    right: PostgresBlockerRelation,
) -> bool:
    return (
        left.database_identity == right.database_identity
        and left.logical_resource == right.logical_resource
        and left.blocker_pid == right.blocker_pid
        and left.waiter_pid == right.waiter_pid
        and left.blocker_backend_type == right.blocker_backend_type
        and left.waiter_backend_type == right.waiter_backend_type
        and left.blocker_application_name == right.blocker_application_name
        and left.waiter_application_name == right.waiter_application_name
    )


def _duration_ms(started_at: float) -> int:
    return max(0, round((monotonic() - started_at) * 1000))


_BLOCKER_QUERY = """
SELECT DISTINCT
    blocker.pid AS blocker_pid,
    waiter.pid AS waiter_pid,
    blocker.backend_type AS blocker_backend_type,
    waiter.backend_type AS waiter_backend_type,
    blocker.application_name AS blocker_application_name,
    waiter.application_name AS waiter_application_name
FROM pg_stat_activity AS waiter
CROSS JOIN LATERAL unnest(pg_blocking_pids(waiter.pid)) AS blocking(pid)
JOIN pg_stat_activity AS blocker ON blocker.pid = blocking.pid
WHERE waiter.wait_event_type = 'Lock'
  AND EXISTS (
      SELECT 1
      FROM pg_locks AS waiter_lock
      JOIN pg_class AS locked_class ON locked_class.oid = waiter_lock.relation
      JOIN pg_namespace AS locked_namespace
        ON locked_namespace.oid = locked_class.relnamespace
      WHERE waiter_lock.pid = waiter.pid
        AND locked_namespace.nspname = :schema
        AND locked_class.relname = :relation
  )
ORDER BY blocker.pid, waiter.pid
"""

_VERIFY_QUERY = """
SELECT
    EXISTS(
        SELECT 1 FROM pg_stat_activity WHERE pid = :blocker_pid
    ) AS blocker_exists,
    EXISTS(
        SELECT 1
        FROM pg_stat_activity AS waiter
        WHERE waiter.pid = :waiter_pid
          AND waiter.wait_event_type = 'Lock'
          AND :blocker_pid = ANY(pg_blocking_pids(waiter.pid))
          AND EXISTS (
              SELECT 1
              FROM pg_locks AS waiter_lock
              JOIN pg_class AS locked_class ON locked_class.oid = waiter_lock.relation
              JOIN pg_namespace AS locked_namespace
                ON locked_namespace.oid = locked_class.relnamespace
              WHERE waiter_lock.pid = waiter.pid
                AND locked_namespace.nspname = :schema
                AND locked_class.relname = :relation
          )
    ) AS waiter_waiting
"""
