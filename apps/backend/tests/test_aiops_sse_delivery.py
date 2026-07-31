# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

"""Redis-fast, PostgreSQL-canonical AIOps SSE delivery tests."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis

from super_ai.api.app import create_app
from super_ai.events.relay import RedisJobEventRelay
from super_ai.events.subscriber import JobEventSubscriber
from super_ai.memory.repositories import BackgroundJobEventRecord
from super_ai.redis_runtime.config import RedisRuntimeSettings


def _event(
    sequence: int, *, job_id: str = "job-a", owner: str = "owner-a"
) -> BackgroundJobEventRecord:
    return BackgroundJobEventRecord(
        id=f"event-{sequence}",
        job_id=job_id,
        owner_user_id=owner,
        sequence=sequence,
        payload={"type": "task.status", "sequence": sequence},
        created_at=datetime.now(timezone.utc),
    )


@dataclass
class FakeBackgroundJobRepository:
    events: list[BackgroundJobEventRecord] = field(default_factory=list)

    async def list_events(
        self, *, owner_user_id: str, job_id: str, after_sequence: int = 0
    ) -> list[BackgroundJobEventRecord]:
        return [
            event
            for event in self.events
            if event.owner_user_id == owner_user_id
            and event.job_id == job_id
            and event.sequence > after_sequence
        ]


class FakeRelay:
    def __init__(self) -> None:
        self.subscriptions: dict[tuple[str, str], asyncio.Event] = {}

    def subscribe(self, *, owner_user_id: str, job_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self.subscriptions[(owner_user_id, job_id)] = event
        return event

    def unsubscribe(self, *, owner_user_id: str, job_id: str, wake_up: asyncio.Event) -> None:
        key = (owner_user_id, job_id)
        if self.subscriptions.get(key) is wake_up:
            del self.subscriptions[key]

    def wake(self, *, owner_user_id: str, job_id: str) -> None:
        event = self.subscriptions.get((owner_user_id, job_id))
        if event is not None:
            event.set()


class FlakyRelay(FakeRelay):
    def __init__(self) -> None:
        super().__init__()
        self.subscribe_attempts = 0

    def subscribe(self, *, owner_user_id: str, job_id: str) -> asyncio.Event:
        self.subscribe_attempts += 1
        if self.subscribe_attempts == 1:
            raise ConnectionError("Redis is unavailable")
        return super().subscribe(owner_user_id=owner_user_id, job_id=job_id)


async def _take(iterator: AsyncIterator[BackgroundJobEventRecord], count: int) -> list[int]:
    result: list[int] = []
    for _ in range(count):
        result.append((await anext(iterator)).sequence)
    return result


@pytest.mark.asyncio
async def test_subscriber_reads_canonical_rows_after_redis_wake_without_duplicates() -> None:
    repository = FakeBackgroundJobRepository(events=[_event(1)])
    relay = FakeRelay()
    subscriber = JobEventSubscriber(repository=repository, relay=relay, poll_interval_seconds=5)
    iterator = subscriber.iter_events(owner_user_id="owner-a", job_id="job-a", after_sequence=0)

    assert await _take(iterator, 1) == [1]
    await asyncio.sleep(0)
    repository.events.extend([_event(1), _event(2), _event(3)])
    relay.wake(owner_user_id="owner-a", job_id="job-a")
    assert await _take(iterator, 2) == [2, 3]

    await iterator.aclose()
    assert relay.subscriptions == {}


@pytest.mark.asyncio
async def test_subscriber_timeout_rechecks_postgresql_when_redis_is_unavailable() -> None:
    repository = FakeBackgroundJobRepository()
    subscriber = JobEventSubscriber(repository=repository, relay=None, poll_interval_seconds=0.001)
    iterator = subscriber.iter_events(owner_user_id="owner-a", job_id="job-a", after_sequence=7)

    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0.01)
    repository.events.append(_event(8))
    assert (await asyncio.wait_for(pending, timeout=1)).sequence == 8
    await iterator.aclose()


@pytest.mark.asyncio
async def test_subscriber_retries_a_failed_redis_registration_with_bounded_polling() -> None:
    repository = FakeBackgroundJobRepository()
    relay = FlakyRelay()
    subscriber = JobEventSubscriber(repository=repository, relay=relay, poll_interval_seconds=0.001)
    iterator = subscriber.iter_events(owner_user_id="owner-a", job_id="job-a", after_sequence=0)
    pending = asyncio.create_task(anext(iterator))

    await asyncio.sleep(0.01)
    assert relay.subscribe_attempts >= 2
    repository.events.append(_event(1))
    relay.wake(owner_user_id="owner-a", job_id="job-a")
    assert (await asyncio.wait_for(pending, timeout=1)).sequence == 1
    await iterator.aclose()
    assert relay.subscriptions == {}


@pytest.mark.asyncio
async def test_subscriber_does_not_receive_another_owner_or_job_wake_up() -> None:
    repository = FakeBackgroundJobRepository()
    relay = FakeRelay()
    subscriber = JobEventSubscriber(repository=repository, relay=relay, poll_interval_seconds=5)
    iterator = subscriber.iter_events(owner_user_id="owner-a", job_id="job-a", after_sequence=0)
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)

    repository.events.append(_event(1, job_id="job-b"))
    relay.wake(owner_user_id="owner-a", job_id="job-b")
    await asyncio.sleep(0.01)
    assert not pending.done()

    repository.events.append(_event(1, job_id="job-a"))
    relay.wake(owner_user_id="owner-a", job_id="job-a")
    assert (await asyncio.wait_for(pending, timeout=1)).sequence == 1
    await iterator.aclose()


@pytest.mark.asyncio
async def test_subscriber_unregisters_local_wake_up_when_cancelled() -> None:
    repository = FakeBackgroundJobRepository()
    relay = FakeRelay()
    subscriber = JobEventSubscriber(repository=repository, relay=relay, poll_interval_seconds=5)
    iterator = subscriber.iter_events(owner_user_id="owner-a", job_id="job-a", after_sequence=0)
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await iterator.aclose()
    assert relay.subscriptions == {}


@pytest.mark.asyncio
async def test_relay_routes_only_valid_owned_wake_metadata_and_removes_only_its_group() -> None:
    prefix = f"task4-relay-{uuid4().hex}"
    settings = RedisRuntimeSettings(url="redis://localhost:6379/15", stream_prefix=prefix)
    client = Redis.from_url(settings.url, decode_responses=True)
    relay = RedisJobEventRelay(client=client, settings=settings, instance_id="instance-a")
    stream_key = f"{prefix}:aiops:events"
    other_group = f"{prefix}:sse:other-instance"
    try:
        await client.xgroup_create(stream_key, other_group, id="$", mkstream=True)
        wake_up = relay.subscribe(owner_user_id="owner-a", job_id="job-a")
        other_wake_up = relay.subscribe(owner_user_id="owner-a", job_id="other-job")
        await relay.start()
        await client.xadd(
            stream_key,
            {
                "owner_id_hash": hashlib.sha256(b"other-owner").hexdigest(),
                "job_id": "other-job",
                "sequence": "1",
            },
        )
        await client.xadd(
            stream_key,
            {
                "owner_id_hash": hashlib.sha256(b"owner-a").hexdigest(),
                "job_id": "job-a",
                "sequence": "8",
                "payload": '{"must":"not be decoded"}',
            },
        )
        await asyncio.wait_for(wake_up.wait(), timeout=2)
        assert not other_wake_up.is_set()
        await relay.stop()

        group_names = {group["name"] for group in await client.xinfo_groups(stream_key)}
        assert relay.group_name not in group_names
        assert other_group in group_names
        assert await client.xlen(stream_key) == 2
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def _register(client: httpx.AsyncClient, email: str) -> dict[str, object]:
    response = await client.post(
        "/auth/register",
        json={"email": email, "password": "password-123", "displayName": "SSE owner"},
    )
    assert response.status_code == 201
    return response.json()["data"]


@pytest.mark.asyncio
async def test_aiops_sse_uses_greater_valid_resume_cursor_and_includes_sequence_ids(
    migrated_database_url: str,
) -> None:
    app = create_app(database_url=migrated_database_url)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        account = await _register(client, "sse-owner@example.com")
        token = account["accessToken"]
        user = account["user"]
        assert isinstance(token, str)
        assert isinstance(user, dict)
        user_id = user["id"]
        assert isinstance(user_id, str)
        repositories = app.state.memory_repositories
        await repositories.diagnostics.create_task(
            owner_user_id=user_id,
            task_id="diagnostic-sse-resume",
            status="succeeded",
            query="resume test",
        )
        jobs = repositories.background_jobs
        assert jobs is not None
        await jobs.enqueue(
            owner_user_id=user_id,
            job_id="job-sse-resume",
            kind="aiops_diagnosis",
            resource_type="aiops_diagnostic",
            resource_id="diagnostic-sse-resume",
        )
        await jobs.append_event(
            owner_user_id=user_id,
            job_id="job-sse-resume",
            payload={"type": "task.status", "status": "running"},
        )
        await jobs.append_event(
            owner_user_id=user_id,
            job_id="job-sse-resume",
            payload={"type": "complete", "status": "succeeded"},
        )
        claimed = await jobs.claim_next(
            worker_id="test-worker",
            lease_expires_at=datetime.now(timezone.utc),
        )
        assert claimed is not None
        await jobs.mark_succeeded(job_id=claimed.id, worker_id="test-worker")
        headers = {"Authorization": f"Bearer {token}", "Last-Event-ID": "1"}
        response = await client.post(
            "/aiops/diagnostics/diagnostic-sse-resume:stream?afterSequence=0",
            headers=headers,
        )
        invalid = await client.post(
            "/aiops/diagnostics/diagnostic-sse-resume:stream?afterSequence=-1",
            headers={"Authorization": f"Bearer {token}"},
        )
        await app.state.background_job_runtime.stop()

    assert response.status_code == 200
    assert "id: 2\n" in response.text
    assert "id: 1\n" not in response.text
    assert "event: complete\n" in response.text
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_aiops_sse_does_not_synthesize_error_when_cursor_covers_stored_error(
    migrated_database_url: str,
) -> None:
    app = create_app(database_url=migrated_database_url)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        account = await _register(client, "sse-stored-error@example.com")
        token = account["accessToken"]
        user = account["user"]
        assert isinstance(token, str)
        assert isinstance(user, dict)
        user_id = user["id"]
        assert isinstance(user_id, str)
        repositories = app.state.memory_repositories
        await repositories.diagnostics.create_task(
            owner_user_id=user_id,
            task_id="diagnostic-sse-stored-error",
            status="failed",
            query="stored error resume test",
        )
        jobs = repositories.background_jobs
        assert jobs is not None
        await jobs.enqueue(
            owner_user_id=user_id,
            job_id="job-sse-stored-error",
            kind="aiops_diagnosis",
            resource_type="aiops_diagnostic",
            resource_id="diagnostic-sse-stored-error",
            max_attempts=1,
        )
        await jobs.append_event(
            owner_user_id=user_id,
            job_id="job-sse-stored-error",
            payload={"type": "error", "error": {"code": "JOB_FAILED"}},
        )
        claimed = await jobs.claim_next(
            worker_id="test-worker",
            lease_expires_at=datetime.now(timezone.utc),
        )
        assert claimed is not None
        failed = await jobs.handle_failure(
            job_id=claimed.id,
            worker_id="test-worker",
            error_message="expected test failure",
            retry_at=datetime.now(timezone.utc),
        )
        assert failed is not None
        assert failed.status == "failed"
        response = await client.post(
            "/aiops/diagnostics/diagnostic-sse-stored-error:stream",
            headers={"Authorization": f"Bearer {token}", "Last-Event-ID": "1"},
        )
        await app.state.background_job_runtime.stop()

    assert response.status_code == 200
    assert response.text == ""


@pytest.mark.asyncio
async def test_aiops_sse_synthetic_failed_error_has_a_stable_unreplayed_id(
    migrated_database_url: str,
) -> None:
    app = create_app(database_url=migrated_database_url)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        account = await _register(client, "sse-synthetic-error@example.com")
        token = account["accessToken"]
        user = account["user"]
        assert isinstance(token, str)
        assert isinstance(user, dict)
        user_id = user["id"]
        assert isinstance(user_id, str)
        repositories = app.state.memory_repositories
        await repositories.diagnostics.create_task(
            owner_user_id=user_id,
            task_id="diagnostic-sse-synthetic-error",
            status="failed",
            query="synthetic error resume test",
        )
        jobs = repositories.background_jobs
        assert jobs is not None
        await jobs.enqueue(
            owner_user_id=user_id,
            job_id="job-sse-synthetic-error",
            kind="aiops_diagnosis",
            resource_type="aiops_diagnostic",
            resource_id="diagnostic-sse-synthetic-error",
            max_attempts=1,
        )
        claimed = await jobs.claim_next(
            worker_id="test-worker",
            lease_expires_at=datetime.now(timezone.utc),
        )
        assert claimed is not None
        failed = await jobs.handle_failure(
            job_id=claimed.id,
            worker_id="test-worker",
            error_message="expected test failure",
            retry_at=datetime.now(timezone.utc),
        )
        assert failed is not None
        first_response = await client.post(
            "/aiops/diagnostics/diagnostic-sse-synthetic-error:stream",
            headers={"Authorization": f"Bearer {token}"},
        )
        resumed_response = await client.post(
            "/aiops/diagnostics/diagnostic-sse-synthetic-error:stream",
            headers={"Authorization": f"Bearer {token}", "Last-Event-ID": "1"},
        )
        await app.state.background_job_runtime.stop()

    assert first_response.status_code == 200
    assert "id: 1\n" in first_response.text
    assert "event: error\n" in first_response.text
    assert resumed_response.status_code == 200
    assert resumed_response.text == ""
