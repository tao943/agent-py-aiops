from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from super_ai.alert_ingestion.repositories import AlertIncidentRecord
from super_ai.recovery.config import (
    DiagnosticSelector,
    PostgresLockResource,
    PostgresRecoveryTarget,
)
from super_ai.recovery.postgres import (
    PostgresBlockerRelation,
    PostgresPreflightExpectation,
    PostgresProbeSnapshot,
    PostgresRecoveryExecutor,
    PostgresTerminationResult,
    PostgresVerificationFacts,
)
from super_ai.recovery.proposal_adapter import lock_relationship_fingerprint
from super_ai.recovery.repository import RecoveryApprovalRecord

NOW = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)


def _target() -> PostgresRecoveryTarget:
    return PostgresRecoveryTarget(
        target_key="agent-py-postgres",
        database_config_key="backend",
        database_identity="agent_py_test",
        diagnostic_selector=DiagnosticSelector(
            "postgresql",
            ("row_lock_blocking",),
            (
                "InspectPostgresLockGraph.blockerEdgeConfirmed",
                "InspectPostgresLockGraph.blockerRole",
                "InspectPostgresLockGraph.lockedResource",
            ),
        ),
        lock_resource_mappings={
            "order_row": PostgresLockResource("order_row", "recovery_test", "orders")
        },
    )


def _fingerprint() -> str:
    target = _target()
    return lock_relationship_fingerprint(
        target_key=target.target_key,
        database_config_key=target.database_config_key,
        database_identity=target.database_identity,
        component="postgresql",
        mechanism="row_lock_blocking",
        required_values={
            "InspectPostgresLockGraph.blockerEdgeConfirmed": True,
            "InspectPostgresLockGraph.blockerRole": "transaction",
            "InspectPostgresLockGraph.lockedResource": "order_row",
        },
    )


def _expectation(**overrides: object) -> PostgresPreflightExpectation:
    values: dict[str, object] = {
        "owner_user_id": "owner-1",
        "incident_id": "incident-1",
        "intent_id": "intent-1",
        "proposal_fingerprint": "a" * 64,
        "relationship_fingerprint": _fingerprint(),
        "logical_resource": "order_row",
    }
    values.update(overrides)
    return PostgresPreflightExpectation(**values)  # type: ignore[arg-type]


def _relation(**overrides: object) -> PostgresBlockerRelation:
    values: dict[str, object] = {
        "database_identity": "agent_py_test",
        "logical_resource": "order_row",
        "blocker_pid": 4101,
        "waiter_pid": 4102,
        "observer_pid": 4199,
        "blocker_backend_type": "client backend",
        "waiter_backend_type": "client backend",
        "blocker_application_name": "order-worker",
        "waiter_application_name": "order-api",
    }
    values.update(overrides)
    return PostgresBlockerRelation(**values)  # type: ignore[arg-type]


class Adapter:
    def __init__(self, snapshots: list[PostgresProbeSnapshot]) -> None:
        self.snapshots = snapshots
        self.probe_resources: list[PostgresLockResource] = []
        self.terminate_pids: list[int] = []
        self.termination = PostgresTerminationResult(True, True, 20)
        self.verification = PostgresVerificationFacts(False, True, False)

    async def probe(self, resource: PostgresLockResource) -> PostgresProbeSnapshot:
        self.probe_resources.append(resource)
        return self.snapshots.pop(0)

    async def terminate(self, pid: int) -> PostgresTerminationResult:
        self.terminate_pids.append(pid)
        return self.termination

    async def verify(
        self, relation: PostgresBlockerRelation, resource: PostgresLockResource
    ) -> PostgresVerificationFacts:
        del relation, resource
        return self.verification


class Incidents:
    def __init__(self, status: str = "resolved") -> None:
        self.status = status

    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        return AlertIncidentRecord(
            incident_id,
            owner_user_id,
            self.status,
            "LockWait",
            "postgresql",
            "critical",
            NOW,
            "diagnostic-1",
        )


def _snapshot(*relations: PostgresBlockerRelation) -> PostgresProbeSnapshot:
    return PostgresProbeSnapshot("agent_py_test", 4199, relations)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "expectation", "reason"),
    [
        (_snapshot(), _expectation(), "postgres_blocker_not_unique"),
        (
            _snapshot(_relation(), _relation(blocker_pid=4201, waiter_pid=4202)),
            _expectation(),
            "postgres_blocker_not_unique",
        ),
        (
            PostgresProbeSnapshot("other_database", 4199, (_relation(),)),
            _expectation(),
            "postgres_database_changed",
        ),
        (
            _snapshot(_relation(blocker_backend_type="autovacuum worker")),
            _expectation(),
            "postgres_blocker_not_client",
        ),
        (
            _snapshot(_relation(blocker_pid=4199)),
            _expectation(),
            "postgres_recovery_connection_targeted",
        ),
        (
            _snapshot(_relation(blocker_pid=4102)),
            _expectation(),
            "postgres_waiter_targeted",
        ),
        (
            _snapshot(_relation(blocker_application_name="agentpy-recovery:worker")),
            _expectation(),
            "postgres_recovery_connection_targeted",
        ),
        (
            _snapshot(_relation(logical_resource="other_row")),
            _expectation(),
            "postgres_lock_relationship_changed",
        ),
        (
            _snapshot(_relation()),
            _expectation(relationship_fingerprint="b" * 64),
            "postgres_lock_relationship_changed",
        ),
    ],
)
async def test_preflight_rejects_unsafe_or_changed_fresh_relations(
    snapshot: PostgresProbeSnapshot,
    expectation: PostgresPreflightExpectation,
    reason: str,
) -> None:
    adapter = Adapter([snapshot])
    executor = PostgresRecoveryExecutor(
        target=_target(),
        adapter=adapter,
        incidents=Incidents(),
        now=lambda: NOW,
    )

    result = await executor.preflight(expectation)

    assert result.allowed is False
    assert result.relation is None
    assert result.safe_reason_code == reason
    assert adapter.terminate_pids == []


@pytest.mark.asyncio
async def test_preflight_uses_unique_fresh_relation_not_untrusted_pid() -> None:
    adapter = Adapter([_snapshot(_relation())])
    executor = PostgresRecoveryExecutor(
        target=_target(), adapter=adapter, incidents=Incidents(), now=lambda: NOW
    )

    result = await executor.preflight(_expectation())

    assert result.allowed is True
    assert result.relation == _relation()
    assert result.relationship_fingerprint == _fingerprint()


def _approval(**overrides: object) -> RecoveryApprovalRecord:
    values: dict[str, object] = {
        "id": "approval-1",
        "intent_id": "intent-1",
        "owner_user_id": "owner-1",
        "approver_user_id": "owner-1",
        "incident_id": "incident-1",
        "proposal_fingerprint": "a" * 64,
        "decision": "approved",
        "created_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=9),
    }
    values.update(overrides)
    return RecoveryApprovalRecord(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approval",
    [
        None,
        _approval(expires_at=NOW),
        _approval(owner_user_id="other-owner"),
        _approval(approver_user_id="other-owner"),
        _approval(incident_id="other-incident"),
        _approval(intent_id="other-intent"),
        _approval(proposal_fingerprint="b" * 64),
    ],
)
async def test_execute_requires_fresh_exact_owner_approval(
    approval: RecoveryApprovalRecord | None,
) -> None:
    relation = _relation()
    adapter = Adapter([_snapshot(relation), _snapshot(relation)])
    executor = PostgresRecoveryExecutor(
        target=_target(), adapter=adapter, incidents=Incidents(), now=lambda: NOW
    )
    preflight = await executor.preflight(_expectation())

    result = await executor.execute_once(_expectation(), preflight, approval)

    assert result.succeeded is False
    assert result.outcome_known is True
    assert adapter.terminate_pids == []


@pytest.mark.asyncio
async def test_execute_reprobes_then_terminates_exact_fresh_blocker_once() -> None:
    relation = _relation()
    second_observer_relation = _relation(observer_pid=4299)
    adapter = Adapter(
        [
            _snapshot(relation),
            PostgresProbeSnapshot(
                "agent_py_test", 4299, (second_observer_relation,)
            ),
        ]
    )
    executor = PostgresRecoveryExecutor(
        target=_target(), adapter=adapter, incidents=Incidents(), now=lambda: NOW
    )
    preflight = await executor.preflight(_expectation())

    result = await executor.execute_once(_expectation(), preflight, _approval())

    assert result.succeeded is True
    assert result.outcome_known is True
    assert adapter.terminate_pids == [4101]


@pytest.mark.asyncio
async def test_unknown_termination_result_is_not_retried() -> None:
    relation = _relation()
    adapter = Adapter([_snapshot(relation), _snapshot(relation)])
    adapter.termination = PostgresTerminationResult(False, False, 25)
    executor = PostgresRecoveryExecutor(
        target=_target(), adapter=adapter, incidents=Incidents(), now=lambda: NOW
    )
    preflight = await executor.preflight(_expectation())

    result = await executor.execute_once(_expectation(), preflight, _approval())

    assert result.succeeded is False
    assert result.outcome_known is False
    assert adapter.terminate_pids == [4101]


@pytest.mark.asyncio
async def test_verify_requires_blocker_waiter_lock_and_incident_checks() -> None:
    relation = _relation()
    adapter = Adapter([])
    executor = PostgresRecoveryExecutor(
        target=_target(), adapter=adapter, incidents=Incidents(), now=lambda: NOW
    )

    result = await executor.verify(_expectation(), relation)

    assert result.passed is True
    assert [item.key for item in result.checks] == [
        "blocker_gone",
        "waiter_progressed",
        "lock_wait_recovered",
        "incident_resolved",
    ]
