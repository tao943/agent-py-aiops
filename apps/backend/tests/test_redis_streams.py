# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from super_ai.memory.repositories import OutboxEventRecord
from super_ai.redis_runtime.config import RedisRuntimeSettings
from super_ai.redis_runtime.streams import RedisStreamJobEventPublisher


def _event(
    *, event_id: str = "event-1", sequence: int = 7, payload: dict[str, object] | None = None,
) -> OutboxEventRecord:
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    return OutboxEventRecord(
        id=event_id,
        owner_user_id="owner-1",
        aggregate_type="background_job",
        aggregate_id="job-1",
        sequence=sequence,
        event_type="job.progress",
        payload=payload or {"status": "running"},
        created_at=now,
        available_at=now,
        published_at=None,
        claimed_by="worker-1",
        claim_expires_at=None,
        attempt_count=1,
        last_error=None,
    )


@pytest.fixture
async def redis_prefix() -> AsyncIterator[str]:
    prefix = f"task3-{uuid4().hex}"
    client = Redis.from_url("redis://localhost:6379/15", decode_responses=True)
    try:
        yield prefix
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_writes_one_canonical_redacted_entry_to_prefixed_stream(
    redis_prefix: str,
) -> None:
    settings = RedisRuntimeSettings(
        url="redis://localhost:6379/15",
        stream_prefix=redis_prefix,
        stream_maxlen=20,
        event_dedupe_ttl_seconds=60,
    )
    client = Redis.from_url(settings.url, decode_responses=True)
    publisher = RedisStreamJobEventPublisher(client, settings)
    event = _event(
        payload={
            "nested": {"authorization": "Bearer secret"},
            "items": [{"apiKey": "secret-key"}],
            "status": "running",
        }
    )
    try:
        await publisher.publish(event)
        entries = await client.xrange(f"{redis_prefix}:aiops:events")
    finally:
        await client.aclose()

    assert entries is not None
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields == {
        "event_id": event.id,
        "owner_id_hash": hashlib.sha256(event.owner_user_id.encode()).hexdigest(),
        "job_id": event.aggregate_id,
        "sequence": "7",
        "event_type": event.event_type,
        "payload": json.dumps(
            {
                "items": [{"apiKey": "[REDACTED]"}],
                "nested": {"authorization": "[REDACTED]"},
                "status": "running",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "created_at": event.created_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_publish_rejects_an_empty_event_type_without_writing_a_stream_entry(
    redis_prefix: str,
) -> None:
    settings = RedisRuntimeSettings(
        url="redis://localhost:6379/15", stream_prefix=redis_prefix, event_dedupe_ttl_seconds=60
    )
    client = Redis.from_url(settings.url, decode_responses=True)
    publisher = RedisStreamJobEventPublisher(client, settings)
    event = _event()
    object.__setattr__(event, "event_type", " ")
    try:
        with pytest.raises(ValueError, match="event_type"):
            await publisher.publish(event)
        assert await client.exists(f"{redis_prefix}:aiops:events") == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_deduplicates_repeated_event_and_keeps_ttl_at_least_configured(
    redis_prefix: str,
) -> None:
    settings = RedisRuntimeSettings(
        url="redis://localhost:6379/15", stream_prefix=redis_prefix, event_dedupe_ttl_seconds=60
    )
    client = Redis.from_url(settings.url, decode_responses=True)
    publisher = RedisStreamJobEventPublisher(client, settings)
    event = _event()
    try:
        await publisher.publish(event)
        await publisher.publish(event)
        await publisher.publish(event)
        assert await client.xlen(f"{redis_prefix}:aiops:events") == 1
        assert await client.ttl(f"{redis_prefix}:aiops:events:dedupe:{event.id}") >= 60
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_uses_bounded_retention_without_creating_another_stream_under_prefix(
    redis_prefix: str,
) -> None:
    settings = RedisRuntimeSettings(
        url="redis://localhost:6379/15", stream_prefix=redis_prefix, stream_maxlen=100
    )
    client = Redis.from_url(settings.url, decode_responses=True)
    publisher = RedisStreamJobEventPublisher(client, settings)
    try:
        for sequence in range(400):
            await publisher.publish(_event(event_id=f"event-{sequence}", sequence=sequence))
        assert await client.xlen(f"{redis_prefix}:aiops:events") <= 200
        keys = {key async for key in client.scan_iter(match=f"{redis_prefix}:*")}
    finally:
        await client.aclose()

    assert f"{redis_prefix}:aiops:events" in keys
    assert all(key == f"{redis_prefix}:aiops:events" or ":dedupe:" in key for key in keys)
