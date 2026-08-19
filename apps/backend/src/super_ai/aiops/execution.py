"""Lease-based idempotent execution coordinator for graph nodes and tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from super_ai.memory.repositories import (
    AiopsExecutionRepository,
    ExecutionClaim,
    ExecutionKind,
    JsonDict,
)


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    task_id: str
    graph_version: str
    node_name: str
    logical_iteration: int
    input_payload: Mapping[str, object]
    execution_kind: ExecutionKind = "node"
    side_effecting: bool = False

    @property
    def input_fingerprint(self) -> str:
        canonical = json.dumps(
            self.input_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def execution_key(self) -> str:
        material = (
            f"{self.task_id}:{self.graph_version}:{self.node_name}:"
            f"{self.logical_iteration}:{self.input_fingerprint}"
        )
        return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    output: JsonDict
    cache_hit: bool
    attempt_count: int


class UnsafeExecutionReplay(RuntimeError):
    pass


class ExecutionCoordinator:
    def __init__(
        self,
        repository: AiopsExecutionRepository,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        self._repository = repository
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    async def run_once(
        self,
        identity: ExecutionIdentity,
        operation: Callable[[], Awaitable[JsonDict]],
        *,
        outcome_known_on_error: bool = True,
    ) -> ExecutionResult:
        now = datetime.now(timezone.utc)
        claim = await self._repository.claim(
            ExecutionClaim(
                execution_key=identity.execution_key,
                execution_kind=identity.execution_kind,
                node_name=identity.node_name,
                logical_iteration=identity.logical_iteration,
                input_fingerprint=identity.input_fingerprint,
                lease_owner=self._worker_id,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                side_effecting=identity.side_effecting,
            )
        )
        if claim.action == "reuse":
            return ExecutionResult(
                output=claim.record.output_payload,
                cache_hit=True,
                attempt_count=claim.record.attempt_count,
            )
        if claim.action == "manual_review":
            raise UnsafeExecutionReplay("uncertain_side_effect_requires_manual_review")
        if claim.action == "wait":
            return await self._wait_for_result(identity.execution_key)
        try:
            output = await operation()
        except Exception:
            await self._repository.fail(
                execution_key=identity.execution_key,
                lease_owner=self._worker_id,
                error_code="operation_failed",
                outcome_known=outcome_known_on_error,
            )
            raise
        completed = await self._repository.complete(
            execution_key=identity.execution_key,
            lease_owner=self._worker_id,
            output=output,
        )
        return ExecutionResult(
            output=completed.output_payload,
            cache_hit=False,
            attempt_count=completed.attempt_count,
        )

    async def _wait_for_result(self, execution_key: str) -> ExecutionResult:
        for _ in range(600):
            record = await self._repository.get(execution_key)
            if record is None:
                break
            if record.status == "completed":
                return ExecutionResult(record.output_payload, True, record.attempt_count)
            if record.status == "uncertain" and record.side_effecting:
                raise UnsafeExecutionReplay(
                    "uncertain_side_effect_requires_manual_review"
                )
            if record.status == "failed":
                raise RuntimeError(record.safe_error_code or "operation_failed")
            await asyncio.sleep(0.01)
        raise TimeoutError("execution_wait_timeout")
