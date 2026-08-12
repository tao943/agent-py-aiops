from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from super_ai.documents.index_client import (
    IndexPollingTimeout,
    IndexProtocolError,
    IndexTaskFailed,
    parse_created_task,
    wait_for_index_task,
)

ENDPOINT = "/knowledge-bases/kb-a/documents/doc-a/index-tasks/task-a"


class FakeClock:
    def __init__(self, values: list[float] | None = None) -> None:
        self._values = iter(values) if values is not None else None
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        if self._values is not None:
            return next(self._values)
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_wait_for_index_task_follows_active_states_to_success() -> None:
    responses = iter(
        [_task_response("pending"), _task_response("running"), _task_response("succeeded")]
    )
    client = _client_for(responses)
    clock = FakeClock()

    task = wait_for_index_task(
        client,
        endpoint=ENDPOINT,
        headers={"Authorization": "Bearer redacted"},
        poll_interval_seconds=1,
        deadline=10.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert task["status"] == "succeeded"
    assert clock.sleeps == [1, 1]


def test_wait_for_index_task_retries_transient_timeout_then_succeeds() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return _task_response("succeeded", request=request)

    clock = FakeClock()
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        task = wait_for_index_task(
            client,
            endpoint=ENDPOINT,
            headers={},
            poll_interval_seconds=1,
            deadline=10.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert task["status"] == "succeeded"
    assert attempts == 2
    assert clock.sleeps == [1]


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_wait_for_index_task_raises_for_terminal_failure(status: str) -> None:
    client = _client_for(iter([_task_response(status, failure_reason="embedding unavailable")]))
    clock = FakeClock()

    with pytest.raises(IndexTaskFailed, match=f"{status}.*embedding unavailable"):
        wait_for_index_task(
            client,
            endpoint=ENDPOINT,
            headers={},
            poll_interval_seconds=0,
            deadline=10.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def test_wait_for_index_task_reports_deadline_and_last_status() -> None:
    client = _client_for(iter([_task_response("running")]))
    clock = FakeClock([0.0, 0.0, 10.0])

    with pytest.raises(IndexPollingTimeout, match="running"):
        wait_for_index_task(
            client,
            endpoint=ENDPOINT,
            headers={},
            poll_interval_seconds=1,
            deadline=10.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"id": "task-a"}},
        {"data": {"id": "task-a", "status": ""}},
        {"data": {"task": {"id": "task-a", "status": "succeeded"}}},
    ],
)
def test_wait_for_index_task_rejects_missing_direct_data_status(payload: object) -> None:
    client = _client_for(iter([_json_response(payload)]))
    clock = FakeClock()

    with pytest.raises(IndexProtocolError, match="status"):
        wait_for_index_task(
            client,
            endpoint=ENDPOINT,
            headers={},
            poll_interval_seconds=0,
            deadline=10.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def test_wait_for_index_task_rejects_unknown_status() -> None:
    client = _client_for(iter([_task_response("accepted")]))
    clock = FakeClock()

    with pytest.raises(IndexProtocolError, match="accepted"):
        wait_for_index_task(
            client,
            endpoint=ENDPOINT,
            headers={},
            poll_interval_seconds=0,
            deadline=10.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def test_parse_created_task_uses_data_task_envelope() -> None:
    task = parse_created_task(
        {"data": {"task": {"id": "task-a", "status": "pending"}, "scheduled": True}}
    )

    assert task == {"id": "task-a", "status": "pending"}


def test_parse_created_task_rejects_query_envelope() -> None:
    with pytest.raises(IndexProtocolError, match="task"):
        parse_created_task({"data": {"id": "task-a", "status": "pending"}})


def _client_for(responses: Iterator[httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        response.request = request
        return response

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _task_response(
    status: str,
    *,
    failure_reason: str | None = None,
    request: httpx.Request | None = None,
) -> httpx.Response:
    task: dict[str, object] = {"id": "task-a", "status": status}
    if failure_reason is not None:
        task["failureReason"] = failure_reason
    return httpx.Response(200, json={"data": task}, request=request)


def _json_response(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)
