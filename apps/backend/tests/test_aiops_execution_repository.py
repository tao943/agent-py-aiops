from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from super_ai.memory.aiops_execution_sqlalchemy import (
    SQLAlchemyAiopsExecutionRepository,
    SQLAlchemyLangGraphCheckpointRepository,
)
from super_ai.memory.database import create_memory_engine, create_memory_session_factory
from super_ai.memory.repositories import (
    CheckpointIdentity,
    ExecutionClaim,
    StoredCheckpoint,
    StoredCheckpointWrite,
    TenantScopeError,
)
from super_ai.memory.sqlalchemy import create_sqlalchemy_memory_repositories


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_owner_and_conflict_keeps_session_usable(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    task_id = f"execution-task-{uuid4().hex}"
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=task_id,
            status="running",
            query="Test execution claim.",
            input_payload={},
        )
        first = SQLAlchemyAiopsExecutionRepository(
            session_factory,
            owner_user_id="benchmark-user",
            task_id=task_id,
            graph_version="aiops-diagnostic-v2",
        )
        second = SQLAlchemyAiopsExecutionRepository(
            session_factory,
            owner_user_id="benchmark-user",
            task_id=task_id,
            graph_version="aiops-diagnostic-v2",
        )
        expires = datetime.now(timezone.utc) + timedelta(minutes=1)
        results = await asyncio.gather(
            first.claim(_claim("same-key", "worker-a", expires)),
            second.claim(_claim("same-key", "worker-b", expires)),
        )
        owner = next(item for item in results if item.action == "acquired")
        waiter = next(item for item in results if item.action == "wait")
        completed = await first.complete(
            execution_key="same-key",
            lease_owner=str(owner.record.lease_owner),
            output={"result": "safe"},
        )
        reused = await second.claim(_claim("same-key", "worker-b", expires))
        readable = await second.get("same-key")
    finally:
        await engine.dispose()

    assert waiter.record.attempt_count == 1
    assert completed.status == "completed"
    assert reused.action == "reuse"
    assert readable is not None and readable.output_payload == {"result": "safe"}


@pytest.mark.asyncio
async def test_uncertain_side_effect_is_never_reclaimed(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    task_id = f"uncertain-task-{uuid4().hex}"
    key = f"recovery-{uuid4().hex}"
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=task_id,
            status="running",
            query="Test uncertain recovery.",
            input_payload={},
        )
        repository = SQLAlchemyAiopsExecutionRepository(
            session_factory,
            owner_user_id="benchmark-user",
            task_id=task_id,
            graph_version="aiops-diagnostic-v2",
        )
        expires = datetime.now(timezone.utc) + timedelta(minutes=1)
        await repository.claim(
            _claim(key, "worker-a", expires, side_effecting=True)
        )
        await repository.fail(
            execution_key=key,
            lease_owner="worker-a",
            error_code="connection_lost_after_dispatch",
            outcome_known=False,
        )
        retried = await repository.claim(
            _claim(key, "worker-b", expires, side_effecting=True)
        )
    finally:
        await engine.dispose()

    assert retried.action == "manual_review"
    assert retried.record.status == "uncertain"


@pytest.mark.asyncio
async def test_expired_side_effect_lease_becomes_manual_review_without_replay(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    task_id = f"expired-recovery-task-{uuid4().hex}"
    key = f"expired-recovery-{uuid4().hex}"
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=task_id,
            status="running",
            query="Test abandoned recovery lease.",
            input_payload={},
        )
        repository = SQLAlchemyAiopsExecutionRepository(
            session_factory,
            owner_user_id="benchmark-user",
            task_id=task_id,
            graph_version="order-pool-auto-closure-v1",
        )
        expired = datetime.now(timezone.utc) - timedelta(seconds=1)
        await repository.claim(
            _claim(key, "crashed-worker", expired, side_effecting=True)
        )

        retried = await repository.claim(
            _claim(
                key,
                "replacement-worker",
                datetime.now(timezone.utc) + timedelta(minutes=1),
                side_effecting=True,
            )
        )
    finally:
        await engine.dispose()

    assert retried.action == "manual_review"
    assert retried.record.status == "uncertain"
    assert retried.record.attempt_count == 1


@pytest.mark.asyncio
async def test_checkpoint_and_writes_round_trip_idempotently(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    task_id = f"checkpoint-task-{uuid4().hex}"
    identity = CheckpointIdentity(
        thread_id=f"aiops:{task_id}:aiops-diagnostic-v2",
        checkpoint_ns="",
        checkpoint_id="0001",
    )
    now = datetime.now(timezone.utc)
    checkpoint = StoredCheckpoint(
        identity=identity,
        parent_checkpoint_id=None,
        checkpoint_type="msgpack",
        checkpoint_blob=b"checkpoint-bytes",
        metadata_type="json",
        metadata_blob=b"metadata-bytes",
        created_at=now,
    )
    write = StoredCheckpointWrite(
        identity=identity,
        write_task_id="langgraph-task-1",
        task_path="pull:executor",
        write_index=0,
        channel="messages",
        value_type="msgpack",
        value_blob=b"write-bytes",
        created_at=now,
    )
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=task_id,
            status="running",
            query="Test checkpoints.",
            input_payload={},
        )
        repository = SQLAlchemyLangGraphCheckpointRepository(
            session_factory,
            owner_user_id="benchmark-user",
            task_id=task_id,
            graph_version="aiops-diagnostic-v2",
        )
        await repository.put_checkpoint(checkpoint)
        await repository.put_checkpoint(checkpoint)
        await repository.put_writes([write, write])
        stored = await repository.get_tuple(identity)
    finally:
        await engine.dispose()

    assert stored is not None
    assert stored.checkpoint == checkpoint
    assert stored.writes == (write,)


@pytest.mark.asyncio
async def test_pending_writes_can_precede_their_checkpoint(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    task_id = f"pending-write-task-{uuid4().hex}"
    identity = CheckpointIdentity(
        thread_id=f"aiops:{task_id}:aiops-diagnostic-v2",
        checkpoint_ns="",
        checkpoint_id="pending-0001",
    )
    now = datetime.now(timezone.utc)
    checkpoint = StoredCheckpoint(
        identity=identity,
        parent_checkpoint_id=None,
        checkpoint_type="msgpack",
        checkpoint_blob=b"checkpoint-bytes",
        metadata_type="json",
        metadata_blob=b"metadata-bytes",
        created_at=now,
    )
    write = StoredCheckpointWrite(
        identity=identity,
        write_task_id="langgraph-task-1",
        task_path="pull:executor",
        write_index=0,
        channel="messages",
        value_type="msgpack",
        value_blob=b"write-before-checkpoint",
        created_at=now,
    )
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        await repositories.diagnostics.create_task(
            owner_user_id="benchmark-user",
            task_id=task_id,
            status="running",
            query="Test pending writes.",
            input_payload={},
        )
        repository = SQLAlchemyLangGraphCheckpointRepository(
            session_factory,
            owner_user_id="benchmark-user",
            task_id=task_id,
            graph_version="aiops-diagnostic-v2",
        )

        await repository.put_writes([write])
        before_checkpoint = await repository.get_tuple(identity)
        await repository.put_checkpoint(checkpoint)
        stored = await repository.get_tuple(identity)
    finally:
        await engine.dispose()

    assert before_checkpoint is None
    assert stored is not None
    assert stored.checkpoint == checkpoint
    assert stored.writes == (write,)


@pytest.mark.asyncio
async def test_pending_write_conflicts_reject_changed_payload_and_scope(
    migrated_database_url: str,
) -> None:
    engine = create_memory_engine(migrated_database_url)
    session_factory = create_memory_session_factory(engine)
    first_task_id = f"pending-write-owner-{uuid4().hex}"
    second_task_id = f"pending-write-intruder-{uuid4().hex}"
    identity = CheckpointIdentity(
        thread_id=f"aiops:{first_task_id}:aiops-diagnostic-v2",
        checkpoint_ns="",
        checkpoint_id="pending-0001",
    )
    now = datetime.now(timezone.utc)
    write = StoredCheckpointWrite(
        identity=identity,
        write_task_id="langgraph-task-1",
        task_path="pull:executor",
        write_index=0,
        channel="messages",
        value_type="msgpack",
        value_blob=b"original-write",
        created_at=now,
    )
    changed_write = StoredCheckpointWrite(
        identity=identity,
        write_task_id=write.write_task_id,
        task_path=write.task_path,
        write_index=write.write_index,
        channel=write.channel,
        value_type=write.value_type,
        value_blob=b"changed-write",
        created_at=now,
    )
    try:
        repositories = create_sqlalchemy_memory_repositories(session_factory)
        for task_id, owner_user_id in (
            (first_task_id, "benchmark-user"),
            (second_task_id, "another-user"),
        ):
            await repositories.diagnostics.create_task(
                owner_user_id=owner_user_id,
                task_id=task_id,
                status="running",
                query="Test pending write isolation.",
                input_payload={},
            )
        owner_repository = SQLAlchemyLangGraphCheckpointRepository(
            session_factory,
            owner_user_id="benchmark-user",
            task_id=first_task_id,
            graph_version="aiops-diagnostic-v2",
        )
        intruder_repository = SQLAlchemyLangGraphCheckpointRepository(
            session_factory,
            owner_user_id="another-user",
            task_id=second_task_id,
            graph_version="aiops-diagnostic-v2",
        )

        await owner_repository.put_writes([write])
        with pytest.raises(TenantScopeError, match="scope or payload"):
            await owner_repository.put_writes([changed_write])
        with pytest.raises(TenantScopeError, match="scope or payload"):
            await intruder_repository.put_writes([write])
    finally:
        await engine.dispose()


def _claim(
    key: str,
    lease_owner: str,
    lease_expires_at: datetime,
    *,
    side_effecting: bool = False,
) -> ExecutionClaim:
    return ExecutionClaim(
        execution_key=key,
        execution_kind="recovery" if side_effecting else "node",
        node_name="policy_gate" if side_effecting else "executor",
        logical_iteration=0,
        input_fingerprint="fingerprint",
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        side_effecting=side_effecting,
    )
