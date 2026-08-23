"""Conflict-safe PostgreSQL repositories for durable AIOps execution."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from super_ai.memory.models import (
    AiopsExecutionModel,
    AiopsLangGraphCheckpointModel,
    AiopsLangGraphWriteModel,
    DiagnosticTaskModel,
    utc_now,
)
from super_ai.memory.repositories import (
    AiopsExecutionRepository,
    AiopsRuntimeRepositoryProvider,
    CheckpointIdentity,
    CheckpointQuery,
    ExecutionClaim,
    ExecutionClaimResult,
    ExecutionKind,
    ExecutionRecord,
    ExecutionStatus,
    JsonDict,
    LangGraphCheckpointRepository,
    StoredCheckpoint,
    StoredCheckpointTuple,
    StoredCheckpointWrite,
    TenantScopeError,
)


class SQLAlchemyAiopsRuntimeRepositoryProvider(AiopsRuntimeRepositoryProvider):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def execution_repository(
        self, *, owner_user_id: str, task_id: str, graph_version: str
    ) -> AiopsExecutionRepository:
        return SQLAlchemyAiopsExecutionRepository(
            self._session_factory,
            owner_user_id=owner_user_id,
            task_id=task_id,
            graph_version=graph_version,
        )

    def checkpoint_repository(
        self, *, owner_user_id: str, task_id: str, graph_version: str
    ) -> LangGraphCheckpointRepository:
        return SQLAlchemyLangGraphCheckpointRepository(
            self._session_factory,
            owner_user_id=owner_user_id,
            task_id=task_id,
            graph_version=graph_version,
        )


class SQLAlchemyAiopsExecutionRepository(AiopsExecutionRepository):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        owner_user_id: str,
        task_id: str,
        graph_version: str,
    ) -> None:
        self._session_factory = session_factory
        self._owner_user_id = owner_user_id
        self._task_id = task_id
        self._graph_version = graph_version

    async def claim(self, request: ExecutionClaim) -> ExecutionClaimResult:
        now = utc_now()
        values: dict[str, object] = {
            "execution_key": request.execution_key,
            "owner_user_id": self._owner_user_id,
            "task_id": self._task_id,
            "graph_version": self._graph_version,
            "execution_kind": request.execution_kind,
            "node_name": request.node_name,
            "logical_iteration": request.logical_iteration,
            "input_fingerprint": request.input_fingerprint,
            "status": "running",
            "attempt_count": 1,
            "lease_owner": request.lease_owner,
            "lease_expires_at": request.lease_expires_at,
            "side_effecting": request.side_effecting,
            "outcome_known": False,
            "output_payload": {},
            "safe_error_code": None,
            "created_at": now,
            "updated_at": now,
        }
        async with self._session_factory() as session:
            await self._require_task(session)
            inserted = await session.scalar(
                postgresql_insert(AiopsExecutionModel)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["execution_key"])
                .returning(AiopsExecutionModel.execution_key)
            )
            await session.commit()

        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(AiopsExecutionModel)
                    .where(AiopsExecutionModel.execution_key == request.execution_key)
                    .with_for_update()
                )
            ).one()
            self._require_scope(row)
            if inserted is not None:
                return ExecutionClaimResult("acquired", _execution_record(row))
            if row.status == "completed":
                return ExecutionClaimResult("reuse", _execution_record(row))
            if row.status == "uncertain" and row.side_effecting:
                return ExecutionClaimResult("manual_review", _execution_record(row))
            lease_valid = (
                row.status == "running"
                and row.lease_expires_at is not None
                and _utc(row.lease_expires_at) > now
            )
            if lease_valid:
                return ExecutionClaimResult("wait", _execution_record(row))
            if row.status == "running" and row.side_effecting:
                row.status = "uncertain"
                row.lease_owner = None
                row.lease_expires_at = None
                row.outcome_known = False
                row.safe_error_code = "worker_lost_after_dispatch"
                row.updated_at = now
                await session.commit()
                return ExecutionClaimResult("manual_review", _execution_record(row))
            row.status = "running"
            row.attempt_count += 1
            row.lease_owner = request.lease_owner
            row.lease_expires_at = request.lease_expires_at
            row.outcome_known = False
            row.safe_error_code = None
            row.updated_at = now
            await session.commit()
            return ExecutionClaimResult("acquired", _execution_record(row))

    async def complete(
        self, *, execution_key: str, lease_owner: str, output: JsonDict
    ) -> ExecutionRecord:
        return await self._finish(
            execution_key=execution_key,
            lease_owner=lease_owner,
            status="completed",
            output=output,
            safe_error_code=None,
            outcome_known=True,
        )

    async def fail(
        self,
        *,
        execution_key: str,
        lease_owner: str,
        error_code: str,
        outcome_known: bool,
    ) -> ExecutionRecord:
        return await self._finish(
            execution_key=execution_key,
            lease_owner=lease_owner,
            status="failed" if outcome_known else "uncertain",
            output={},
            safe_error_code=error_code,
            outcome_known=outcome_known,
        )

    async def get(self, execution_key: str) -> ExecutionRecord | None:
        async with self._session_factory() as session:
            row = await session.get(AiopsExecutionModel, execution_key)
        if row is None:
            return None
        self._require_scope(row)
        return _execution_record(row)

    async def _finish(
        self,
        *,
        execution_key: str,
        lease_owner: str,
        status: ExecutionStatus,
        output: JsonDict,
        safe_error_code: str | None,
        outcome_known: bool,
    ) -> ExecutionRecord:
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    select(AiopsExecutionModel)
                    .where(AiopsExecutionModel.execution_key == execution_key)
                    .with_for_update()
                )
            ).one()
            self._require_scope(row)
            if row.status != "running" or row.lease_owner != lease_owner:
                raise RuntimeError("execution_lease_mismatch")
            row.status = status
            row.output_payload = output
            row.safe_error_code = safe_error_code
            row.outcome_known = outcome_known
            row.lease_expires_at = None
            row.updated_at = utc_now()
            await session.commit()
            return _execution_record(row)

    async def _require_task(self, session: AsyncSession) -> None:
        row = await session.get(DiagnosticTaskModel, self._task_id)
        if row is None or row.owner_user_id != self._owner_user_id:
            raise TenantScopeError("Diagnostic task is not accessible.")

    def _require_scope(self, row: AiopsExecutionModel) -> None:
        if (
            row.owner_user_id != self._owner_user_id
            or row.task_id != self._task_id
            or row.graph_version != self._graph_version
        ):
            raise TenantScopeError("AIOps execution is outside the repository scope.")


class SQLAlchemyLangGraphCheckpointRepository(LangGraphCheckpointRepository):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        owner_user_id: str,
        task_id: str,
        graph_version: str,
    ) -> None:
        self._session_factory = session_factory
        self._owner_user_id = owner_user_id
        self._task_id = task_id
        self._graph_version = graph_version

    async def get_tuple(
        self, identity: CheckpointIdentity
    ) -> StoredCheckpointTuple | None:
        async with self._session_factory() as session:
            row = await session.get(
                AiopsLangGraphCheckpointModel,
                (identity.thread_id, identity.checkpoint_ns, identity.checkpoint_id),
            )
            if row is None:
                return None
            self._require_checkpoint_scope(row)
            writes = list(
                (
                    await session.scalars(
                        _writes_query(identity)
                        .where(
                            AiopsLangGraphWriteModel.owner_user_id
                            == self._owner_user_id,
                            AiopsLangGraphWriteModel.diagnostic_task_id
                            == self._task_id,
                            AiopsLangGraphWriteModel.graph_version
                            == self._graph_version,
                        )
                        .order_by(
                            AiopsLangGraphWriteModel.write_task_id,
                            AiopsLangGraphWriteModel.task_path,
                            AiopsLangGraphWriteModel.write_index,
                        )
                    )
                ).all()
            )
        return StoredCheckpointTuple(
            checkpoint=_stored_checkpoint(row),
            writes=tuple(_stored_write(item) for item in writes),
        )

    async def list_tuples(
        self, query: CheckpointQuery
    ) -> Sequence[StoredCheckpointTuple]:
        statement = select(AiopsLangGraphCheckpointModel).where(
            AiopsLangGraphCheckpointModel.owner_user_id == self._owner_user_id,
            AiopsLangGraphCheckpointModel.task_id == self._task_id,
            AiopsLangGraphCheckpointModel.graph_version == self._graph_version,
            AiopsLangGraphCheckpointModel.thread_id == query.thread_id,
        )
        if query.checkpoint_ns:
            statement = statement.where(
                AiopsLangGraphCheckpointModel.checkpoint_ns == query.checkpoint_ns
            )
        if query.before_checkpoint_id is not None:
            statement = statement.where(
                AiopsLangGraphCheckpointModel.checkpoint_id < query.before_checkpoint_id
            )
        statement = statement.order_by(
            AiopsLangGraphCheckpointModel.checkpoint_id.desc()
        )
        if query.limit is not None:
            statement = statement.limit(query.limit)
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        tuples: list[StoredCheckpointTuple] = []
        for row in rows:
            item = await self.get_tuple(
                CheckpointIdentity(row.thread_id, row.checkpoint_ns, row.checkpoint_id)
            )
            if item is not None:
                tuples.append(item)
        return tuples

    async def put_checkpoint(self, record: StoredCheckpoint) -> None:
        values = {
            "thread_id": record.identity.thread_id,
            "checkpoint_ns": record.identity.checkpoint_ns,
            "checkpoint_id": record.identity.checkpoint_id,
            "owner_user_id": self._owner_user_id,
            "task_id": self._task_id,
            "graph_version": self._graph_version,
            "parent_checkpoint_id": record.parent_checkpoint_id,
            "checkpoint_type": record.checkpoint_type,
            "checkpoint_blob": record.checkpoint_blob,
            "metadata_type": record.metadata_type,
            "metadata_blob": record.metadata_blob,
            "created_at": record.created_at,
        }
        async with self._session_factory() as session:
            await self._require_task(session)
            statement = postgresql_insert(AiopsLangGraphCheckpointModel).values(
                **values
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["thread_id", "checkpoint_ns", "checkpoint_id"],
                    set_={
                        "parent_checkpoint_id": statement.excluded.parent_checkpoint_id,
                        "checkpoint_type": statement.excluded.checkpoint_type,
                        "checkpoint_blob": statement.excluded.checkpoint_blob,
                        "metadata_type": statement.excluded.metadata_type,
                        "metadata_blob": statement.excluded.metadata_blob,
                        "created_at": statement.excluded.created_at,
                    },
                    where=and_(
                        AiopsLangGraphCheckpointModel.owner_user_id
                        == self._owner_user_id,
                        AiopsLangGraphCheckpointModel.task_id == self._task_id,
                        AiopsLangGraphCheckpointModel.graph_version
                        == self._graph_version,
                    ),
                )
            )
            await session.commit()
        stored = await self.get_tuple(record.identity)
        if stored is None or not _same_checkpoint_payload(stored.checkpoint, record):
            raise TenantScopeError("Checkpoint identity belongs to another scope or payload.")

    async def put_writes(self, records: Sequence[StoredCheckpointWrite]) -> None:
        if not records:
            return
        values = [
            {
                "thread_id": item.identity.thread_id,
                "checkpoint_ns": item.identity.checkpoint_ns,
                "checkpoint_id": item.identity.checkpoint_id,
                "write_task_id": item.write_task_id,
                "task_path": item.task_path,
                "write_index": item.write_index,
                "diagnostic_task_id": self._task_id,
                "owner_user_id": self._owner_user_id,
                "graph_version": self._graph_version,
                "channel": item.channel,
                "value_type": item.value_type,
                "value_blob": item.value_blob,
                "created_at": item.created_at,
            }
            for item in records
        ]
        async with self._session_factory() as session:
            await self._require_task(session)
            await session.execute(
                postgresql_insert(AiopsLangGraphWriteModel)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "thread_id",
                        "checkpoint_ns",
                        "checkpoint_id",
                        "write_task_id",
                        "task_path",
                        "write_index",
                    ]
                )
            )
            for record in records:
                row = await session.get(
                    AiopsLangGraphWriteModel,
                    (
                        record.identity.thread_id,
                        record.identity.checkpoint_ns,
                        record.identity.checkpoint_id,
                        record.write_task_id,
                        record.task_path,
                        record.write_index,
                    ),
                )
                if not _same_checkpoint_write(
                    row,
                    record,
                    owner_user_id=self._owner_user_id,
                    task_id=self._task_id,
                    graph_version=self._graph_version,
                ):
                    await session.rollback()
                    raise TenantScopeError(
                        "Checkpoint write identity belongs to another scope or payload."
                    )
            await session.commit()

    async def _require_task(self, session: AsyncSession) -> None:
        row = await session.get(DiagnosticTaskModel, self._task_id)
        if row is None or row.owner_user_id != self._owner_user_id:
            raise TenantScopeError("Diagnostic task is not accessible.")

    def _require_checkpoint_scope(self, row: AiopsLangGraphCheckpointModel) -> None:
        if (
            row.owner_user_id != self._owner_user_id
            or row.task_id != self._task_id
            or row.graph_version != self._graph_version
        ):
            raise TenantScopeError("Checkpoint is outside the repository scope.")


def _writes_query(identity: CheckpointIdentity):
    return select(AiopsLangGraphWriteModel).where(
        AiopsLangGraphWriteModel.thread_id == identity.thread_id,
        AiopsLangGraphWriteModel.checkpoint_ns == identity.checkpoint_ns,
        AiopsLangGraphWriteModel.checkpoint_id == identity.checkpoint_id,
    )


def _same_checkpoint_payload(
    stored: StoredCheckpoint,
    requested: StoredCheckpoint,
) -> bool:
    """Ignore insertion time while preserving identity, scope, and blob integrity."""
    return (
        stored.identity == requested.identity
        and stored.parent_checkpoint_id == requested.parent_checkpoint_id
        and stored.checkpoint_type == requested.checkpoint_type
        and stored.checkpoint_blob == requested.checkpoint_blob
        and stored.metadata_type == requested.metadata_type
        and stored.metadata_blob == requested.metadata_blob
    )


def _same_checkpoint_write(
    stored: AiopsLangGraphWriteModel | None,
    requested: StoredCheckpointWrite,
    *,
    owner_user_id: str,
    task_id: str,
    graph_version: str,
) -> bool:
    """Validate idempotent pending writes without requiring a parent checkpoint."""
    return bool(
        stored is not None
        and stored.owner_user_id == owner_user_id
        and stored.diagnostic_task_id == task_id
        and stored.graph_version == graph_version
        and stored.thread_id == requested.identity.thread_id
        and stored.checkpoint_ns == requested.identity.checkpoint_ns
        and stored.checkpoint_id == requested.identity.checkpoint_id
        and stored.write_task_id == requested.write_task_id
        and stored.task_path == requested.task_path
        and stored.write_index == requested.write_index
        and stored.channel == requested.channel
        and stored.value_type == requested.value_type
        and stored.value_blob == requested.value_blob
    )


def _execution_record(row: AiopsExecutionModel) -> ExecutionRecord:
    return ExecutionRecord(
        execution_key=row.execution_key,
        owner_user_id=row.owner_user_id,
        task_id=row.task_id,
        graph_version=row.graph_version,
        execution_kind=cast(ExecutionKind, row.execution_kind),
        node_name=row.node_name,
        logical_iteration=row.logical_iteration,
        input_fingerprint=row.input_fingerprint,
        status=cast(ExecutionStatus, row.status),
        attempt_count=row.attempt_count,
        lease_owner=row.lease_owner,
        lease_expires_at=_utc(row.lease_expires_at) if row.lease_expires_at else None,
        side_effecting=row.side_effecting,
        outcome_known=row.outcome_known,
        output_payload=dict(row.output_payload),
        safe_error_code=row.safe_error_code,
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _stored_checkpoint(row: AiopsLangGraphCheckpointModel) -> StoredCheckpoint:
    return StoredCheckpoint(
        identity=CheckpointIdentity(row.thread_id, row.checkpoint_ns, row.checkpoint_id),
        parent_checkpoint_id=row.parent_checkpoint_id,
        checkpoint_type=row.checkpoint_type,
        checkpoint_blob=bytes(row.checkpoint_blob),
        metadata_type=row.metadata_type,
        metadata_blob=bytes(row.metadata_blob),
        created_at=_utc(row.created_at),
    )


def _stored_write(row: AiopsLangGraphWriteModel) -> StoredCheckpointWrite:
    return StoredCheckpointWrite(
        identity=CheckpointIdentity(row.thread_id, row.checkpoint_ns, row.checkpoint_id),
        write_task_id=row.write_task_id,
        task_path=row.task_path,
        write_index=row.write_index,
        channel=row.channel,
        value_type=row.value_type,
        value_blob=bytes(row.value_blob),
        created_at=_utc(row.created_at),
    )


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
