from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from super_ai.events.outbox import OutboxDispatcher
from super_ai.memory.repositories import OutboxEventRecord


def _event(*, event_id: str = "event-1", attempt_count: int = 1) -> OutboxEventRecord:
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    return OutboxEventRecord(
        id=event_id,
        owner_user_id="owner-1",
        aggregate_type="background_job",
        aggregate_id="job-1",
        sequence=7,
        event_type="job.progress",
        payload={"status": "running"},
        created_at=now,
        available_at=now,
        published_at=None,
        claimed_by="worker-1",
        claim_expires_at=None,
        attempt_count=attempt_count,
        last_error=None,
    )


class FakeRepository:
    def __init__(self, events: list[OutboxEventRecord]) -> None:
        self.events = events
        self.published: list[str] = []
        self.released: list[tuple[str, str, datetime]] = []

    async def claim_batch(
        self, *, worker_id: str, limit: int, lease_seconds: int
    ) -> list[OutboxEventRecord]:
        return self.events[:limit]

    async def mark_published(self, event_id: str, *, published_at: datetime) -> None:
        self.published.append(event_id)

    async def release(self, event_id: str, *, error: str, available_at: datetime) -> None:
        self.released.append((event_id, error, available_at))


class FakePublisher:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish(self, event: OutboxEventRecord) -> None:
        self.events.append(event.id)


class SelectivePublisher(FakePublisher):
    def __init__(self, failures: dict[str, BaseException]) -> None:
        super().__init__()
        self.failures = failures

    async def publish(self, event: OutboxEventRecord) -> None:
        self.events.append(event.id)
        failure = self.failures.get(event.id)
        if failure is not None:
            raise failure


class BlockingRepository(FakeRepository):
    def __init__(self) -> None:
        super().__init__([])
        self.claim_started = asyncio.Event()
        self.cancelled = False

    async def claim_batch(
        self, *, worker_id: str, limit: int, lease_seconds: int
    ) -> list[OutboxEventRecord]:
        self.claim_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return []


@pytest.mark.asyncio
async def test_run_once_marks_event_published_only_after_publisher_acknowledges() -> None:
    repository = FakeRepository([_event()])
    publisher = FakePublisher()
    dispatcher = OutboxDispatcher(repository=repository, publisher=publisher, worker_id="worker-1")

    await dispatcher.run_once()

    assert publisher.events == ["event-1"]
    assert repository.published == ["event-1"]


@pytest.mark.asyncio
async def test_run_once_releases_only_the_failed_event_with_capped_backoff_and_sanitized_error(
) -> None:
    repository = FakeRepository([_event(event_id="event-1", attempt_count=4)])
    publisher = SelectivePublisher({"event-1": RuntimeError("password=not-for-logs")})
    dispatcher = OutboxDispatcher(
        repository=repository,
        publisher=publisher,
        worker_id="worker-1",
        minimum_backoff_seconds=2,
        maximum_backoff_seconds=8,
    )
    before = datetime.now(timezone.utc)

    await dispatcher.run_once()

    assert repository.published == []
    assert len(repository.released) == 1
    event_id, error, available_at = repository.released[0]
    assert event_id == "event-1"
    assert error == "RuntimeError"
    assert before.timestamp() + 8 <= available_at.timestamp() <= before.timestamp() + 9


@pytest.mark.asyncio
async def test_run_once_continues_after_a_poison_event() -> None:
    repository = FakeRepository([_event(event_id="poison"), _event(event_id="healthy")])
    publisher = SelectivePublisher({"poison": RuntimeError("boom")})
    dispatcher = OutboxDispatcher(repository=repository, publisher=publisher, worker_id="worker-1")

    await dispatcher.run_once()

    assert publisher.events == ["poison", "healthy"]
    assert repository.published == ["healthy"]
    assert [event_id for event_id, _, _ in repository.released] == ["poison"]


@pytest.mark.asyncio
async def test_run_once_releases_current_event_and_reraises_cancellation() -> None:
    repository = FakeRepository([_event(event_id="published"), _event(event_id="cancelled")])
    publisher = SelectivePublisher({"cancelled": asyncio.CancelledError()})
    dispatcher = OutboxDispatcher(repository=repository, publisher=publisher, worker_id="worker-1")

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.run_once()

    assert repository.published == ["published"]
    assert [event_id for event_id, _, _ in repository.released] == ["cancelled"]


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_awaits_its_cancelled_task() -> None:
    repository = BlockingRepository()
    dispatcher = OutboxDispatcher(
        repository=repository,
        publisher=FakePublisher(),
        worker_id="worker-1",
        poll_interval_seconds=60,
    )

    dispatcher.start()
    dispatcher.start()
    await repository.claim_started.wait()
    await dispatcher.stop()

    assert repository.cancelled is True
