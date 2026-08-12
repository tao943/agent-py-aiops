"""Typed response parsing and bounded polling for document index tasks."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import cast

import httpx

ACTIVE_STATUSES = frozenset({"pending", "running"})
SUCCESS_STATUS = "succeeded"
FAILURE_STATUSES = frozenset({"failed", "cancelled"})


class IndexPollingError(RuntimeError):
    """Base error for index-task client failures."""


class IndexTaskFailed(IndexPollingError):
    """An index task entered a terminal failure state."""


class IndexPollingTimeout(IndexPollingError):
    """An index task did not finish before its deadline."""


class IndexProtocolError(IndexPollingError):
    """The backend response did not match the index-task contract."""


def parse_created_task(payload: object) -> dict[str, object]:
    """Parse the nested task returned by the index-task creation endpoint."""
    envelope = _object(payload, "response")
    data = _object(envelope.get("data"), "data")
    return _task(data.get("task"), location="data.task")


def wait_for_index_task(
    client: httpx.Client,
    *,
    endpoint: str,
    headers: Mapping[str, str],
    poll_interval_seconds: float,
    deadline: float,
    transient_retry_limit: int = 2,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Poll a query endpoint until the task succeeds or reaches a terminal error."""
    if poll_interval_seconds < 0:
        raise ValueError("poll_interval_seconds must be non-negative")
    if transient_retry_limit < 0:
        raise ValueError("transient_retry_limit must be non-negative")

    last_status: str | None = None
    transient_failures = 0
    while monotonic() < deadline:
        try:
            response = client.get(endpoint, headers=headers)
            response.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            transient_failures += 1
            if transient_failures > transient_retry_limit:
                raise IndexPollingError(
                    f"Index task query exceeded {transient_retry_limit} transient retries."
                ) from exc
            _sleep_before_next_poll(
                poll_interval_seconds, deadline=deadline, monotonic=monotonic, sleep=sleep
            )
            continue

        transient_failures = 0
        task = _parse_queried_task(response)
        status = _required_string(task.get("status"), "data.status")
        last_status = status
        if status == SUCCESS_STATUS:
            return task
        if status in FAILURE_STATUSES:
            reason = task.get("failureReason")
            reason_suffix = f": {reason}" if isinstance(reason, str) and reason else ""
            task_id = task.get("id")
            task_label = task_id if isinstance(task_id, str) and task_id else "unknown"
            raise IndexTaskFailed(
                f"Index task {task_label} entered terminal status {status}{reason_suffix}"
            )
        if status not in ACTIVE_STATUSES:
            raise IndexProtocolError(f"Unknown index task status: {status}")
        _sleep_before_next_poll(
            poll_interval_seconds, deadline=deadline, monotonic=monotonic, sleep=sleep
        )

    suffix = f"; last status was {last_status}" if last_status is not None else ""
    raise IndexPollingTimeout(f"Timed out while waiting for index task{suffix}.")


def _parse_queried_task(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise IndexProtocolError("The backend returned invalid JSON.") from exc
    envelope = _object(payload, "response")
    return _task(envelope.get("data"), location="data")


def _task(value: object, *, location: str) -> dict[str, object]:
    task = _object(value, location)
    _required_string(task.get("status"), f"{location}.status")
    _required_string(task.get("id"), f"{location}.id")
    return task


def _object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise IndexProtocolError(f"Expected an object at {location}.")
    return cast(dict[str, object], value)


def _required_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise IndexProtocolError(f"Expected a non-empty string at {location}.")
    return value


def _sleep_before_next_poll(
    interval: float,
    *,
    deadline: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    remaining = deadline - monotonic()
    if remaining > 0:
        sleep(min(interval, remaining))
