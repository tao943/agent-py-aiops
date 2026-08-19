"""Tenant-scoped async LangGraph saver backed by PostgreSQL BYTEA records."""
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from super_ai.memory.repositories import (
    CheckpointIdentity,
    CheckpointQuery,
    LangGraphCheckpointRepository,
    StoredCheckpoint,
    StoredCheckpointTuple,
    StoredCheckpointWrite,
)


class PostgresDiagnosticCheckpointSaver(BaseCheckpointSaver[int]):
    def __init__(
        self,
        repository: LangGraphCheckpointRepository,
        *,
        task_id: str,
        graph_version: str,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._thread_id = f"aiops:{task_id}:{graph_version}"

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, checkpoint_ns, checkpoint_id = self._identity_parts(config)
        if checkpoint_id:
            stored = await self._repository.get_tuple(
                CheckpointIdentity(thread_id, checkpoint_ns, checkpoint_id)
            )
        else:
            items = await self._repository.list_tuples(
                CheckpointQuery(thread_id=thread_id, checkpoint_ns=checkpoint_ns, limit=1)
            )
            stored = items[0] if items else None
        return self._decode_tuple(stored) if stored is not None else None

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        effective = config or {"configurable": {"thread_id": self._thread_id}}
        thread_id, checkpoint_ns, _ = self._identity_parts(effective)
        before_id = None
        if before is not None:
            _, _, before_id = self._identity_parts(before)
        items = await self._repository.list_tuples(
            CheckpointQuery(
                thread_id=thread_id,
                checkpoint_ns=checkpoint_ns,
                before_checkpoint_id=before_id or None,
                limit=limit,
            )
        )
        for item in items:
            decoded = self._decode_tuple(item)
            if filter and not all(
                decoded.metadata.get(key) == value for key, value in filter.items()
            ):
                continue
            yield decoded

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Mapping[str, str | int | float],
    ) -> RunnableConfig:
        del new_versions
        thread_id, checkpoint_ns, parent_checkpoint_id = self._identity_parts(config)
        checkpoint_id = str(checkpoint["id"])
        checkpoint_type, checkpoint_blob = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_blob = self.serde.dumps_typed(metadata)
        await self._repository.put_checkpoint(
            StoredCheckpoint(
                identity=CheckpointIdentity(thread_id, checkpoint_ns, checkpoint_id),
                parent_checkpoint_id=parent_checkpoint_id or None,
                checkpoint_type=checkpoint_type,
                checkpoint_blob=checkpoint_blob,
                metadata_type=metadata_type,
                metadata_blob=metadata_blob,
                created_at=datetime.now(timezone.utc),
            )
        )
        configurable = dict(cast(Mapping[str, object], config.get("configurable") or {}))
        configurable.update(
            {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        )
        return {**config, "configurable": configurable}

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, checkpoint_ns, checkpoint_id = self._identity_parts(config)
        if not checkpoint_id:
            raise ValueError("LangGraph writes require checkpoint_id.")
        now = datetime.now(timezone.utc)
        records: list[StoredCheckpointWrite] = []
        for index, (channel, value) in enumerate(writes):
            value_type, value_blob = self.serde.dumps_typed(value)
            records.append(
                StoredCheckpointWrite(
                    identity=CheckpointIdentity(
                        thread_id, checkpoint_ns, checkpoint_id
                    ),
                    write_task_id=task_id,
                    task_path=task_path,
                    write_index=index,
                    channel=channel,
                    value_type=value_type,
                    value_blob=value_blob,
                    created_at=now,
                )
            )
        await self._repository.put_writes(records)

    def _identity_parts(self, config: RunnableConfig) -> tuple[str, str, str]:
        configurable = cast(Mapping[str, object], config.get("configurable") or {})
        thread_id = str(configurable.get("thread_id") or self._thread_id)
        if thread_id != self._thread_id:
            raise ValueError("Checkpoint thread is outside the diagnostic task scope.")
        return (
            thread_id,
            str(configurable.get("checkpoint_ns") or ""),
            str(configurable.get("checkpoint_id") or ""),
        )

    def _decode_tuple(self, stored: StoredCheckpointTuple) -> CheckpointTuple:
        checkpoint = cast(
            Checkpoint,
            self.serde.loads_typed(
                (stored.checkpoint.checkpoint_type, stored.checkpoint.checkpoint_blob)
            ),
        )
        metadata = cast(
            CheckpointMetadata,
            self.serde.loads_typed(
                (stored.checkpoint.metadata_type, stored.checkpoint.metadata_blob)
            ),
        )
        config: RunnableConfig = {
            "configurable": {
                "thread_id": stored.checkpoint.identity.thread_id,
                "checkpoint_ns": stored.checkpoint.identity.checkpoint_ns,
                "checkpoint_id": stored.checkpoint.identity.checkpoint_id,
            }
        }
        parent_config = (
            {
                "configurable": {
                    "thread_id": stored.checkpoint.identity.thread_id,
                    "checkpoint_ns": stored.checkpoint.identity.checkpoint_ns,
                    "checkpoint_id": stored.checkpoint.parent_checkpoint_id,
                }
            }
            if stored.checkpoint.parent_checkpoint_id
            else None
        )
        pending_writes = [
            (
                item.write_task_id,
                item.channel,
                self.serde.loads_typed((item.value_type, item.value_blob)),
            )
            for item in stored.writes
        ]
        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=cast(RunnableConfig | None, parent_config),
            pending_writes=pending_writes,
        )
